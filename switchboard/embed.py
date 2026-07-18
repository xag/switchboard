"""The embeddable channel: a hosted app self-hosts its own switchboard.

The loopback daemon only reaches apps on the user's machine. A hosted app on a remote server
can't dial 127.0.0.1 on the user's box, and shouldn't install anything there. So it embeds
the broker instead: it holds a `Channel` in its own process and exposes the user-side MCP
surface over HTTP. The user adds the app's URL as a remote MCP connector once — that is the
consent gate, since only the user can add a connector to their own client — and the same
pairing handshake runs in-band. No local daemon, no relay we operate.

The core is the same `Switchboard` the daemon wraps; only the two faces change:

- The **app** reaches the core in-process — `Channel.ask` calls the core directly, no wire.
- The **user's client** reaches it over the network — `Channel.mcp_app()` returns an ASGI
  app serving the identical five tools (`_tools`) over streamable-HTTP, which the app mounts
  in its own server.

Both faces touch one `Switchboard`, so a request the app asks and a `take`/`deliver` the
client makes meet on the same futures and queue — provided they share the app server's
asyncio loop, which a mounted ASGI app and its request handlers do. Every request is still
written ahead before it is serviced; point `record=` at the app's own store.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Awaitable, Callable, Optional

from mcp.server.fastmcp import FastMCP

from . import _tools, discovery, wal
from .core import Record, Switchboard


class NotPaired(Exception):
    """A request was asked before the user authorized the channel. Carries the code to show
    the user, who matches it in their client to authorize the connector's pairing."""

    def __init__(self, code: str, pairing_id: str) -> None:
        super().__init__("channel is not paired — the user must authorize it once")
        self.code = code
        self.pairing_id = pairing_id


class Denied(Exception):
    """The user declined the pairing in their client."""


class _CoreHandlers:
    """The user-side verbs bound straight to the in-process core — no wire between the MCP
    tool and the broker, because in a hosted app they are the same process."""

    def __init__(self, board: Switchboard) -> None:
        self._board = board

    def pairings(self) -> dict[str, Any]:
        return self._board.pending_pairings()

    def authorize(self, pairing_id: str, code: str) -> dict[str, Any]:
        return self._board.authorize({"pairing_id": pairing_id, "code": code})

    def deny(self, pairing_id: str) -> dict[str, Any]:
        return self._board.deny({"pairing_id": pairing_id})

    async def take(self) -> dict[str, Any]:
        # Non-blocking: the remote client polls, so a take never holds an HTTP request open.
        return await self._board.take({"wait": 0})

    def deliver(self, request_id: str, result: Any) -> dict[str, Any]:
        return self._board.deliver({"request_id": request_id, "result": result})


class Channel:
    """A hosted app's own switchboard channel — one app, one broker, in the app's process.

    Construct it once at startup, mount `mcp_app()` in the server, and call `ask` from the
    app's request handlers. `record` is the write-ahead sink; it defaults to switchboard's
    shared log, but a hosted app should pass its own so the record lives with the app.
    """

    def __init__(self, app: str, record: Optional[Record] = None) -> None:
        name = (app or "").strip()
        if not name:
            raise ValueError("a channel must name its app")
        self.app = name
        self.board = Switchboard(record=record if record is not None else wal.append)
        self._token: Optional[str] = None
        self._pairing_id: Optional[str] = None

    # -- pairing (the app owns how it shows the code) --------------------------------

    @property
    def paired(self) -> bool:
        return self._token is not None

    def begin_pairing(self) -> str:
        """Open a pairing and return the code to show the user, who authorizes it in their
        client. Idempotent while pending — the same code comes back until it is used."""
        r = self.board.pair_request({"app": self.app})
        self._pairing_id = r["pairing_id"]
        return r["code"]

    def pairing_status(self) -> str:
        """`pending` | `authorized` | `denied` — and, once authorized, the token is cached
        so `ask` can proceed."""
        if not self._pairing_id:
            return "none"
        s = self.board.pair_status({"pairing_id": self._pairing_id})
        if s.get("status") == "authorized" and self._token is None:
            self._token = s["token"]
        return s.get("status", "none")

    async def await_paired(self, wait: float = 300.0, poll: float = 0.5) -> None:
        """Block until the user authorizes the pairing (or denies / times out). The user's
        act happens over the MCP connector — `switchboard_authorize` on their side."""
        if not self._pairing_id:
            raise RuntimeError("begin_pairing first")
        deadline = time.time() + wait
        while time.time() < deadline:
            status = self.pairing_status()
            if status == "authorized":
                return
            if status == "denied":
                raise Denied("pairing was declined")
            await asyncio.sleep(poll)
        raise TimeoutError(f"pairing not authorized within {wait}s")

    # -- requests --------------------------------------------------------------------

    async def ask(self, request: Any, wait: float = 120.0) -> Any:
        """Send a request to the user's live session and return its result. Raises
        `NotPaired` (carrying a code to show) if the user has not authorized the channel
        yet — the first request patches through to a pairing, exactly as the local wire."""
        r = self.board.ask({"token": self._token, "app": self.app, "request": request})
        if not r.get("ok"):
            if r.get("status") == "unpaired":
                self._pairing_id = r["pairing_id"]
                raise NotPaired(r["code"], r["pairing_id"])
            raise RuntimeError(r.get("error", "ask failed"))
        rid = r["request_id"]
        res = await self.board.await_result({"request_id": rid, "wait": wait})
        if res.get("status") == "done":
            return res["result"]
        raise TimeoutError(f"no result for {rid} within {wait}s")

    async def pair_and_ask(self, request: Any, show_code: Callable[[str], Any],
                           pair_wait: float = 300.0, ask_wait: float = 120.0) -> Any:
        """The common case: ensure the channel is paired (showing the code via `show_code`,
        which may be a coroutine), then ask. `show_code` is how the app surfaces the code to
        the user in its own UI — the app owns that."""
        if not self.paired:
            code = self.begin_pairing()
            shown = show_code(code)
            if asyncio.iscoroutine(shown):
                await shown
            await self.await_paired(wait=pair_wait)
        return await self.ask(request, wait=ask_wait)

    # -- the remote MCP surface the app mounts ---------------------------------------

    def build_mcp(self, name: str = "switchboard") -> FastMCP:
        """The user-side tools bound to this channel's core, with flight recording armed."""
        mcp = FastMCP(name)
        _tools.register(mcp, _CoreHandlers(self.board))
        try:
            from flight_recorder import install_mcp

            from .boundary import boundary
            install_mcp(boundary(), mcp, directory=str(discovery.HOME / "flight"))
        except Exception as e:  # noqa: BLE001 — recording must not stop the channel
            print(f"[switchboard] WARNING: flight recording not armed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
        return mcp

    def mcp_app(self, name: str = "switchboard"):
        """A streamable-HTTP ASGI app exposing the user-side surface. Mount it in the app's
        own server (Starlette/FastAPI `mount`, or run it standalone); the URL of its `/mcp`
        endpoint is what the user adds as a remote MCP connector."""
        return self.build_mcp(name).streamable_http_app()
