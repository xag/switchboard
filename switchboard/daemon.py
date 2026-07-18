"""The switchboard daemon: one shared broker, holding pairings and the request queue.

Asyncio TCP server on 127.0.0.1, ephemeral port published to the discovery file. Three
parties meet here: apps (`pair_request`, `ask`, `await_result`), the user authorizing in
the client (`pending_pairings`, `authorize`, `deny`), and the client servicing requests
(`take`, `deliver`). The daemon never inspects a payload — it routes and records (the
ledger's transports-not-adjudicates and write-ahead lines).

Run blocking with `python -m switchboard daemon`; the SessionStart hook spawns that
detached. `ensure_running()` is the idempotent front door: it returns the live endpoint,
starting the daemon only if none answers.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import __version__, discovery, wal
from .protocol import Endpoint, V

# Pairing codes expire if left unauthorized this long — a stale pending pairing should not
# linger as an authorizable slot.
PAIR_TTL = 300.0


@dataclass
class Pending:
    pairing_id: str
    app: str
    code: str
    created: float
    status: str = "pending"  # pending | authorized | denied
    token: Optional[str] = None


@dataclass
class Req:
    request_id: str
    app: str
    request: Any
    status: str = "queued"  # queued | taken | done
    result: Any = None
    fut: Optional[asyncio.Future] = None


class Switchboard:
    def __init__(self) -> None:
        self.nonce = secrets.token_hex(8)
        self.pending: dict[str, Pending] = {}     # pairing_id -> Pending (and authorized)
        self.by_token: dict[str, Pending] = {}    # token -> Pending
        self.reqs: dict[str, Req] = {}            # request_id -> Req
        self.inbox: "asyncio.Queue[str]" = asyncio.Queue()
        self._rn = 0
        self._pn = 0

    # -- id minting (deterministic per lifetime; readable in the WAL) ---------------

    def _next_request_id(self) -> str:
        self._rn += 1
        return f"r{self._rn}"

    def _next_pairing_id(self) -> str:
        self._pn += 1
        return f"p{self._pn}"

    # -- pairing --------------------------------------------------------------------

    def _open_pairing(self, app: str) -> Pending:
        """Idempotent while pending: an app re-requesting keeps its code and slot."""
        for p in self.pending.values():
            if p.app == app and p.status == "pending" \
                    and time.time() - p.created < PAIR_TTL:
                return p
        p = Pending(self._next_pairing_id(), app, f"{secrets.randbelow(1_000_000):06d}",
                    time.time())
        self.pending[p.pairing_id] = p
        wal.append({"ts": time.time(), "event": "pair_request",
                    "pairing_id": p.pairing_id, "app": app})
        return p

    def pair_request(self, msg: dict) -> dict:
        app = (msg.get("app") or "").strip()
        if not app:
            return {"ok": False, "error": "an app must name itself to pair"}
        p = self._open_pairing(app)
        return {"ok": True, "pairing_id": p.pairing_id, "code": p.code}

    def pending_pairings(self) -> dict:
        now = time.time()
        out = [{"pairing_id": p.pairing_id, "app": p.app, "code": p.code}
               for p in self.pending.values()
               if p.status == "pending" and now - p.created < PAIR_TTL]
        return {"ok": True, "pairings": out}

    def pair_status(self, msg: dict) -> dict:
        p = self.pending.get(msg.get("pairing_id", ""))
        if not p:
            return {"ok": False, "error": "no such pairing"}
        return {"ok": True, "status": p.status,
                **({"token": p.token} if p.status == "authorized" else {})}

    def authorize(self, msg: dict) -> dict:
        """The user's act, relayed by the MCP tool. The code matched on both sides is the
        mechanical guard: authorizing a pairing_id with a code that is not its own is
        refused, so a mix-up between two pending pairings cannot cross the wires."""
        p = self.pending.get(msg.get("pairing_id", ""))
        if not p:
            return {"ok": False, "error": "no such pairing"}
        if p.status != "pending":
            return {"ok": False, "error": f"pairing already {p.status}"}
        if str(msg.get("code", "")) != p.code:
            return {"ok": False, "error": "code mismatch - the app shown is not this one"}
        p.status = "authorized"
        p.token = secrets.token_urlsafe(24)
        self.by_token[p.token] = p
        wal.append({"ts": time.time(), "event": "authorize",
                    "pairing_id": p.pairing_id, "app": p.app})
        return {"ok": True, "token": p.token, "app": p.app}

    def deny(self, msg: dict) -> dict:
        p = self.pending.get(msg.get("pairing_id", ""))
        if not p:
            return {"ok": False, "error": "no such pairing"}
        p.status = "denied"
        wal.append({"ts": time.time(), "event": "deny",
                    "pairing_id": p.pairing_id, "app": p.app})
        return {"ok": True}

    # -- requests -------------------------------------------------------------------

    def ask(self, msg: dict) -> dict:
        """An app's request. Without a valid token the first request patches through to a
        pairing: the app gets a code to show and awaits authorization, then retries."""
        token = msg.get("token")
        p = self.by_token.get(token) if token else None
        if p is None:
            app = (msg.get("app") or "").strip()
            if not app:
                return {"ok": False, "error": "unpaired, and no app name to pair with"}
            opened = self._open_pairing(app)
            return {"ok": False, "status": "unpaired",
                    "pairing_id": opened.pairing_id, "code": opened.code}
        rid = self._next_request_id()
        req = Req(rid, p.app, msg.get("request"))
        # Write-ahead: the request is durable before it is queued to be serviced.
        wal.append({"ts": time.time(), "event": "request", "request_id": rid,
                    "app": p.app, "request": req.request})
        req.fut = asyncio.get_running_loop().create_future()
        self.reqs[rid] = req
        self.inbox.put_nowait(rid)
        return {"ok": True, "request_id": rid}

    async def await_result(self, msg: dict) -> dict:
        rid = msg.get("request_id", "")
        req = self.reqs.get(rid)
        if not req:
            return {"ok": False, "error": "no such request"}
        if req.status == "done":
            return {"ok": True, "status": "done", "result": req.result}
        wait = float(msg.get("wait", 60.0))
        try:
            await asyncio.wait_for(asyncio.shield(req.fut), timeout=wait)
        except asyncio.TimeoutError:
            return {"ok": True, "status": req.status}
        return {"ok": True, "status": "done", "result": req.result}

    async def take(self, msg: dict) -> dict:
        """The client servicing side pulls the next queued request. Long-poll: waits up to
        `wait` seconds for one to arrive, so an idle session can hold the line open."""
        wait = float(msg.get("wait", 25.0))
        try:
            rid = self.inbox.get_nowait() if not self.inbox.empty() \
                else await asyncio.wait_for(self.inbox.get(), timeout=wait)
        except asyncio.TimeoutError:
            return {"ok": True, "empty": True}
        req = self.reqs.get(rid)
        if not req:
            return {"ok": True, "empty": True}
        req.status = "taken"
        return {"ok": True, "request_id": rid, "app": req.app, "request": req.request}

    def deliver(self, msg: dict) -> dict:
        rid = msg.get("request_id", "")
        req = self.reqs.get(rid)
        if not req:
            return {"ok": False, "error": "no such request"}
        req.result = msg.get("result")
        req.status = "done"
        wal.append({"ts": time.time(), "event": "result", "request_id": rid,
                    "result": req.result})
        if req.fut and not req.fut.done():
            req.fut.set_result(req.result)
        return {"ok": True}

    # -- dispatch -------------------------------------------------------------------

    async def dispatch(self, msg: dict) -> dict:
        verb = msg.get("verb")
        if verb == V.PING:
            return {"ok": True, "nonce": self.nonce, "pid": os.getpid(),
                    "version": __version__}
        if verb == V.PAIR_REQUEST:
            return self.pair_request(msg)
        if verb == V.PENDING_PAIRINGS:
            return self.pending_pairings()
        if verb == V.PAIR_STATUS:
            return self.pair_status(msg)
        if verb == V.AUTHORIZE:
            return self.authorize(msg)
        if verb == V.DENY:
            return self.deny(msg)
        if verb == V.ASK:
            return self.ask(msg)
        if verb == V.AWAIT_RESULT:
            return await self.await_result(msg)
        if verb == V.TAKE:
            return await self.take(msg)
        if verb == V.DELIVER:
            return self.deliver(msg)
        return {"ok": False, "error": f"unknown verb {verb!r}"}


# -- the server ---------------------------------------------------------------------

async def _serve_conn(board: Switchboard, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
    try:
        line = await reader.readline()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            resp: dict = {"ok": False, "error": f"bad frame: {e}"}
        else:
            resp = await board.dispatch(msg)
        writer.write((json.dumps(resp) + "\n").encode("utf-8"))
        await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _amain() -> None:
    board = Switchboard()
    server = await asyncio.start_server(
        lambda r, w: _serve_conn(board, r, w), host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    info = {"host": "127.0.0.1", "port": port, "pid": os.getpid(),
            "nonce": board.nonce, "version": __version__, "started_at": time.time()}
    discovery.write(info)
    _log(f"switchboard up on 127.0.0.1:{port} pid={os.getpid()} nonce={board.nonce}")
    try:
        async with server:
            await server.serve_forever()
    finally:
        discovery.clear(only_nonce=board.nonce)


def _log(line: str) -> None:
    try:
        discovery.ensure_home()
        with open(discovery.LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def run() -> int:
    """Blocking entry — the process the hook spawns."""
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    return 0


# -- idempotent spawn ---------------------------------------------------------------

def spawn() -> None:
    """Start the daemon as a detached background process that outlives its spawner.

    DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP on Windows, start_new_session on POSIX;
    stdio to DEVNULL so a full pipe never blocks the child (the hook gotcha). The daemon
    writes its own log to ~/.switchboard/daemon.log."""
    discovery.ensure_home()
    args = [sys.executable, "-m", "switchboard", "daemon"]
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL, "close_fds": True,
                          "cwd": str(Path.home())}
    if os.name == "nt":
        kw["creationflags"] = (subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)


def ensure_running(timeout: float = 8.0) -> dict:
    """Return the live discovery info, starting the daemon only if none answers. Safe to
    call from every session start: the second caller finds the first's daemon and spawns
    nothing."""
    info = discovery.alive()
    if info:
        return info
    spawn()
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = discovery.alive(timeout=1.0)
        if info:
            return info
        time.sleep(0.15)
    raise RuntimeError("switchboard daemon did not come up within "
                       f"{timeout}s — see {discovery.LOG}")
