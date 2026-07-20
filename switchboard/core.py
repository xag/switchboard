"""The broker core: pairings and the request queue, with no transport of its own.

One `Switchboard` holds the whole state machine — open a pairing, authorize it against a
matched code, accept a request (write-ahead first), hand it to whoever services it, return
the result. It speaks in dict-in / dict-out verb handlers and `dispatch`, so a transport is
just a thin shell around it: the loopback-TCP daemon wraps it for same-machine apps, and the
embeddable library (`embed.py`) wraps the same instance behind a remote MCP surface a hosted
app self-hosts. The core knows nothing of either — it routes and records, it does not
adjudicate payloads (the ledger's transports-not-adjudicates and write-ahead lines).

`record` is the write-ahead sink, injected so the deployment decides where the log lives: the
daemon keeps the shared `~/.switchboard/wal.jsonl`; a hosted app points it at its own store.
The mark is always written before a request is dispatched — that never becomes optional.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import __version__, wal
from .protocol import V

# Pairing codes expire if left unauthorized this long — a stale pending pairing should not
# linger as an authorizable slot.
PAIR_TTL = 300.0

Record = Callable[[dict], None]


@dataclass
class Pending:
    pairing_id: str
    app: str
    code: str
    created: float
    status: str = "pending"  # pending | preauthorized | authorized | denied
    token: Optional[str] = None
    secret: Optional[str] = None  # set only while preauthorized; consumed by the claim


@dataclass
class Req:
    request_id: str
    app: str
    request: Any
    urgency: str = "idle"  # idle (wait for the turn to end) | turn (surface mid-turn)
    status: str = "queued"  # queued | taken | done
    result: Any = None
    fut: Optional[asyncio.Future] = None


class Switchboard:
    def __init__(self, record: Optional[Record] = None) -> None:
        self.nonce = secrets.token_hex(8)
        self._record: Record = record if record is not None else wal.append
        self.pending: dict[str, Pending] = {}     # pairing_id -> Pending (and authorized)
        self.by_token: dict[str, Pending] = {}    # token -> Pending
        self.by_secret: dict[str, Pending] = {}   # spawn secret -> preauthorized Pending
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
        self._record({"ts": time.time(), "event": "pair_request",
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
        self._record({"ts": time.time(), "event": "authorize",
                      "pairing_id": p.pairing_id, "app": p.app})
        return {"ok": True, "token": p.token, "app": p.app}

    def deny(self, msg: dict) -> dict:
        p = self.pending.get(msg.get("pairing_id", ""))
        if not p:
            return {"ok": False, "error": "no such pairing"}
        p.status = "denied"
        self._record({"ts": time.time(), "event": "deny",
                      "pairing_id": p.pairing_id, "app": p.app})
        return {"ok": True}

    def preauthorize(self, msg: dict) -> dict:
        """The user's session, about to spawn an app itself, mints a spawn secret. Minting
        is the authorization — the session chose to launch this app — so the app redeems
        the secret with `pair_claim` and no code is shown to anyone. The secret is single
        use and expires like a pending code, so a leaked environment cannot pair later."""
        app = (msg.get("app") or "").strip()
        if not app:
            return {"ok": False, "error": "name the app the secret is for"}
        p = Pending(self._next_pairing_id(), app, f"{secrets.randbelow(1_000_000):06d}",
                    time.time(), status="preauthorized",
                    secret=secrets.token_urlsafe(24))
        self.pending[p.pairing_id] = p
        self.by_secret[p.secret] = p
        self._record({"ts": time.time(), "event": "preauthorize",
                      "pairing_id": p.pairing_id, "app": app})
        return {"ok": True, "pairing_id": p.pairing_id, "secret": p.secret}

    def pair_claim(self, msg: dict) -> dict:
        """An app redeems a spawn secret for a token. One redemption per secret: the claim
        consumes it, and a stale secret (older than PAIR_TTL) is refused."""
        p = self.by_secret.pop(str(msg.get("secret", "")), None)
        if p is None or p.status != "preauthorized":
            return {"ok": False, "error": "no such spawn secret"}
        if time.time() - p.created >= PAIR_TTL:
            p.status = "denied"
            return {"ok": False, "error": "spawn secret expired"}
        p.status = "authorized"
        p.secret = None
        p.token = secrets.token_urlsafe(24)
        self.by_token[p.token] = p
        self._record({"ts": time.time(), "event": "claim",
                      "pairing_id": p.pairing_id, "app": p.app})
        return {"ok": True, "token": p.token, "app": p.app}

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
        urgency = msg.get("urgency", "idle")
        if urgency not in ("idle", "turn"):
            return {"ok": False, "error": f"urgency must be 'idle' or 'turn', not {urgency!r}"}
        req = Req(rid, p.app, msg.get("request"), urgency=urgency)
        # Write-ahead: the request is durable before it is queued to be serviced.
        self._record({"ts": time.time(), "event": "request", "request_id": rid,
                      "app": p.app, "request": req.request, "urgency": urgency})
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
        return {"ok": True, "request_id": rid, "app": req.app, "request": req.request,
                "urgency": req.urgency}

    def deliver(self, msg: dict) -> dict:
        rid = msg.get("request_id", "")
        req = self.reqs.get(rid)
        if not req:
            return {"ok": False, "error": "no such request"}
        req.result = msg.get("result")
        req.status = "done"
        self._record({"ts": time.time(), "event": "result", "request_id": rid,
                      "result": req.result})
        if req.fut and not req.fut.done():
            req.fut.set_result(req.result)
        return {"ok": True}

    def queue_status(self) -> dict:
        """The one cheap fact the hooks poll and the listener watches: what is waiting,
        and how urgently. `queued` counts requests not yet taken; `interject` the subset
        an app marked urgency='turn'; `pairings` the codes awaiting the user; `waiting`
        names them, so a listener can tell a new request from one it already announced."""
        queued = [r for r in self.reqs.values() if r.status == "queued"]
        now = time.time()
        pairings = sum(1 for p in self.pending.values()
                       if p.status == "pending" and now - p.created < PAIR_TTL)
        return {"ok": True, "queued": len(queued),
                "interject": sum(1 for r in queued if r.urgency == "turn"),
                "pairings": pairings,
                "apps": sorted({r.app for r in queued}),
                "waiting": [{"request_id": r.request_id, "app": r.app,
                             "urgency": r.urgency} for r in queued]}

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
        if verb == V.PREAUTHORIZE:
            return self.preauthorize(msg)
        if verb == V.PAIR_CLAIM:
            return self.pair_claim(msg)
        if verb == V.QUEUE_STATUS:
            return self.queue_status()
        if verb == V.ASK:
            return self.ask(msg)
        if verb == V.AWAIT_RESULT:
            return await self.await_result(msg)
        if verb == V.TAKE:
            return await self.take(msg)
        if verb == V.DELIVER:
            return self.deliver(msg)
        return {"ok": False, "error": f"unknown verb {verb!r}"}
