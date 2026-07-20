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
  app serving the identical tools (`_tools`) over streamable-HTTP, which the app mounts
  in its own server.

Both faces touch one `Switchboard`, so a request the app asks and a `take`/`deliver` the
client makes meet on the same futures and queue — provided they share the app server's
asyncio loop, which a mounted ASGI app and its request handlers do. Every request is still
written ahead before it is serviced; point `record=` at the app's own store.
"""

from __future__ import annotations

import asyncio
import secrets
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

    def preauthorize(self, app: str) -> dict[str, Any]:
        return self._board.preauthorize({"app": app})

    def waiting(self) -> dict[str, Any]:
        return self._board.queue_status()

    async def take(self) -> dict[str, Any]:
        # Non-blocking: the remote client polls, so a take never holds an HTTP request open.
        return await self._board.take({"wait": 0})

    def deliver(self, request_id: str, result: Any) -> dict[str, Any]:
        return self._board.deliver({"request_id": request_id, "result": result})


class _RequireBearer:
    """ASGI middleware: every request to the user-side surface must carry the token.

    It wraps the whole app rather than guarding each tool, so `/mcp` and `/waiting` are
    covered by one rule and a tool added later cannot be forgotten. The comparison is
    constant-time; a token checked with `==` leaks its prefix to a patient caller."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        offered = ""
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                offered = value.decode("latin-1")
                break
        if not secrets.compare_digest(offered, self._expected):
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body",
                        "body": b'{"ok": false, "error": "this switchboard channel '
                                b'requires a bearer token"}'})
            return
        await self._app(scope, receive, send)


class Channel:
    """A hosted app's own switchboard channel — one app, one broker, in the app's process.

    Construct it once at startup, mount `mcp_app()` in the server, and call `ask` from the
    app's request handlers. `record` is the write-ahead sink; it defaults to switchboard's
    shared log, but a hosted app should pass its own so the record lives with the app.
    """

    def __init__(self, app: str, record: Optional[Record] = None,
                 public_url: Optional[str] = None,
                 auth_token: Optional[str] = None) -> None:
        name = (app or "").strip()
        if not name:
            raise ValueError("a channel must name its app")
        self.app = name
        self.board = Switchboard(record=record if record is not None else wal.append)
        self._token: Optional[str] = None
        self._pairing_id: Optional[str] = None
        # Where this channel is reachable from outside, if the app knows (it may sit
        # behind a proxy). Only used to write an exact command into the instructions —
        # a watcher told to poll "your URL" is a watcher that never gets armed.
        self.public_url = (public_url or "").rstrip("/") or None
        # The shared secret the user's client must present. The pairing code says WHICH
        # APP is pairing; it says nothing about who is calling the user-side tools. On a
        # reachable URL without this, anyone can take a request (reading its payload and
        # denying it to the real session) and deliver a forged answer back to the app.
        self.auth_token = auth_token or None

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

    def claim(self, secret: str) -> None:
        """Redeem a spawn secret the user's session minted (switchboard_preauthorize) and
        handed to this app out of band — the pre-approved pairing, no code shown."""
        r = self.board.pair_claim({"secret": secret})
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "claim failed"))
        self._token = r["token"]

    def pairing_prompt(self) -> str:
        """A paste-able pairing request for the app to put behind a share sheet or copy
        button. The user launching it in their client is the acceptance — carrying the
        code over proves the same possession the eyeball-match does."""
        code = self.begin_pairing()
        return (f"The app '{self.app}' asks to pair with this session's switchboard: "
                f"if I sent this, accept with switchboard_authorize("
                f"pairing_id='{self._pairing_id}', code='{code}'); otherwise deny it.")

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

    async def ask(self, request: Any, wait: float = 120.0, urgency: str = "idle") -> Any:
        """Send a request to the user's live session and return its result. Raises
        `NotPaired` (carrying a code to show) if the user has not authorized the channel
        yet — the first request patches through to a pairing, exactly as the local wire.
        `urgency` is how the session should surface it: 'idle' waits for the turn to end,
        'turn' asks to be interjected mid-turn."""
        r = self.board.ask({"token": self._token, "app": self.app, "request": request,
                            "urgency": urgency})
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

    def _register_waiting_route(self, mcp: FastMCP) -> None:
        """A plain `GET /waiting` returning the same read-only queue status as the tool.

        MCP is a poor thing to poll from a shell: it wants an initialize handshake and a
        session header before it will answer a question. A watcher has to be armable with
        one line and no dependencies, so the same fact is served as ordinary JSON.

        It carries counts, app names, request ids and urgencies — never a payload. What an
        app sent is the session's business, and a status endpoint has no business
        repeating it."""
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        @mcp.custom_route("/waiting", methods=["GET"])
        async def waiting(request: Request) -> JSONResponse:  # noqa: ARG001
            return JSONResponse(self.board.queue_status())

    def register_on(self, mcp: FastMCP) -> None:
        """Put this channel's user-side tools on an existing MCP server. An app that already
        serves its own surface carries the channel on it — the client that spawned the app
        services requests through the same connection, no separate connector. This is the
        primary shape: switchboard replaces sampling on the app's own surface. `build_mcp`
        uses it for the standalone (separate-surface) case."""
        _tools.register(mcp, _CoreHandlers(self.board))

    def instructions(self) -> str:
        """What the connector itself tells the agent — including how to start listening.

        A hosted app's user installs nothing: no package, no hooks, no SessionStart
        context. The only thing that reaches their client is this connector, so if the
        instruction to watch the channel does not travel here, it does not travel at all,
        and the app's requests sit unseen until the user happens to ask.

        It names a concrete command, not a capability. `/waiting` is plain HTTP returning
        plain JSON, so the watcher is a curl loop with nothing to install — an instruction
        that needs a package the user does not have is the same as no instruction."""
        where = self.public_url or "<this connector's base URL, minus /mcp>"
        # The token goes into the command because otherwise the command cannot run: the
        # agent has no other way to learn it. These instructions are served over the
        # authenticated surface, so only a caller who already holds the token can read
        # them — but it does land in the session transcript, which the app should weigh.
        auth = f' -H "Authorization: Bearer {self.auth_token}"' if self.auth_token else ""
        return (
            f"This is a switchboard channel for the app '{self.app}': it lets the app "
            f"send you work to do, and you answer it from this session.\n\n"
            f"Servicing: call switchboard_waiting to see what is queued (it takes "
            f"nothing), switchboard_take to pull one request, then switchboard_deliver "
            f"to return the result. Authorize a first-time pairing with "
            f"switchboard_pairings and switchboard_authorize, matching the code the app "
            f"shows the user.\n\n"
            f"IMPORTANT - so requests reach you while you sit idle: nothing here can "
            f"interrupt you, so start a background watcher ONCE, early in the session, "
            f"and keep it running. In Claude Code use the Monitor tool (persistent) "
            f"with this command:\n\n"
            f"    while true; do curl -sf{auth} {where}/waiting "
            f"| grep -o '\"queued\": *[1-9][0-9]*' && echo 'switchboard: work is queued "
            f"- call switchboard_take'; sleep 5; done\n\n"
            f"Each line it prints reaches you even when idle; then service the queue as "
            f"above. Without it, this app's requests wait until the user next types.")

    def build_mcp(self, name: str = "switchboard") -> FastMCP:
        """The user-side tools bound to this channel's core, with flight recording armed."""
        mcp = FastMCP(name, instructions=self.instructions())
        self.register_on(mcp)
        self._register_waiting_route(mcp)
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
        endpoint is what the user adds as a remote MCP connector.

        With `auth_token` set, every request must carry it as a bearer token. Without one
        the surface is open to whoever can reach the URL, which is only defensible if the
        app puts its own authentication in front of the mount — so say so out loud rather
        than let a public deployment happen quietly."""
        app = self.build_mcp(name).streamable_http_app()
        if self.auth_token:
            return _RequireBearer(app, self.auth_token)
        print(f"[switchboard] WARNING: channel '{self.app}' is mounted with no "
              f"auth_token. Anyone who can reach this URL can read requests, deny them "
              f"to your session, and deliver forged answers. Pass auth_token=..., or "
              f"put your own authentication in front of the mount.", file=sys.stderr)
        return app
