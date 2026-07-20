"""The app-side door: how any app reaches the channel.

An app names itself, pairs once (showing the code the user will match in their client), then
sends requests and waits for results. Liveness is a first-class fact: if the switchboard
dies, the app goes `stale` — its token belonged to that daemon's lifetime, so a restarted
switchboard means re-pairing, and the app can see that rather than hang on a dead channel.

This is a thin, dependency-free client over the JSON wire; an app in any language can
reimplement it from `protocol.py`. The primitives are `begin_pairing` / `await_pairing`
(the app owns how it shows the code to the user) and `ask`; `pair_and_ask` composes them
for the common case.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import discovery, protocol
from .protocol import V

# The convention for handing a spawn secret to an app the session launches itself.
SECRET_ENV = "SWITCHBOARD_SECRET"


class Stale(Exception):
    """No live switchboard is reachable — the channel is down."""


class Denied(Exception):
    """The user declined the pairing."""


class NotPaired(Exception):
    """A request was sent before the app paired. Carries the code to show the user."""

    def __init__(self, code: str, pairing_id: str) -> None:
        super().__init__("app is not paired to the switchboard")
        self.code = code
        self.pairing_id = pairing_id


class App:
    def __init__(self, name: str, secret: Optional[str] = None,
                 token_store: Optional[Path] = None, remember: bool = True,
                 autostart: bool = True) -> None:
        self.name = name
        self._autostart = autostart
        self._info: Optional[dict] = None
        self._pairing_id: Optional[str] = None
        # A spawn secret, if the session that launched this app minted one. Passed
        # explicitly or found in the environment; `ask` redeems it on first use.
        self._secret = secret if secret is not None else os.environ.get(SECRET_ENV)
        # Where this app keeps the token it was issued, so a pairing survives a restart
        # of either side. Pass remember=False for an app that would rather ask every time.
        self._store = token_store if token_store is not None else self._default_store()
        self._remember = remember
        self.token: Optional[str] = self._load_token() if remember else None

    def _default_store(self) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.name)
        return discovery.HOME / "apps" / f"{safe}.token"

    def _load_token(self) -> Optional[str]:
        try:
            return self._store.read_text("utf-8").strip() or None
        except OSError:
            return None

    def _save_token(self, token: str) -> None:
        """Keep the token so the next run is recognised. It is a file in the user's own
        home, readable by anything running as them — the registry docstring is explicit
        that this cannot separate processes, only remember decisions."""
        if not self._remember:
            return
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            self._store.write_text(token, encoding="utf-8")
        except OSError:
            pass  # an app that cannot persist still works; it just re-pairs next time

    # -- liveness -------------------------------------------------------------------

    @property
    def stale(self) -> bool:
        """True if the switchboard this app knew is no longer answering (dead, or replaced
        by a different daemon whose nonce differs)."""
        if self._info is None:
            return discovery.alive() is None
        return discovery.alive(info=self._info) is None

    def _call(self, verb: str, **fields: Any) -> dict:
        """One frame, with a dead channel reported as `Stale` rather than a socket error.

        A daemon that dies mid-request drops the connection, and an app that asked for a
        result got a bare ConnectionResetError traceback for its trouble. `Stale` is the
        fact the app can actually act on — the same one `stale` reports — so both routes
        to 'the channel is gone' arrive as the same exception."""
        try:
            return protocol.call(self._endpoint(), verb, **fields)
        except OSError as e:
            raise Stale(f"the switchboard stopped answering: {e}") from e

    def _endpoint(self) -> protocol.Endpoint:
        info = discovery.alive()
        if not info and self._autostart:
            # The daemon is a shared, idempotent, per-user service, and the user-side MCP
            # server already starts it on demand. An app finding none may do the same:
            # a pairing now survives a restart, so recovery is a restart and a retry
            # rather than the whole ceremony again.
            try:
                from . import daemon
                info = daemon.ensure_running()
            except Exception:  # noqa: BLE001 — report the channel as down, not this
                info = None
        if not info:
            raise Stale("no live switchboard")
        # A restarted daemon no longer invalidates the token: the user's decision is kept
        # in the registry, so the new daemon recognises what the old one issued. If it
        # does not (a forgotten or revoked app), `ask` patches through to a pairing —
        # which is the right place to find out, rather than discarding a good token here.
        self._info = info
        return discovery.endpoint_of(info)

    # -- pairing --------------------------------------------------------------------

    def begin_pairing(self) -> str:
        """Open a pairing and return the code to show the user. The user matches it in
        their client and authorizes there."""
        r = self._call(V.PAIR_REQUEST, app=self.name)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "pair_request failed"))
        self._pairing_id = r["pairing_id"]
        return r["code"]

    def claim(self, secret: Optional[str] = None) -> str:
        """Redeem a spawn secret for a token — the pairing the session pre-approved when
        it launched this app. Single use; a wrong or expired secret raises."""
        s = secret or self._secret
        if not s:
            raise RuntimeError("no spawn secret to claim")
        r = self._call(V.PAIR_CLAIM, secret=s)
        self._secret = None  # consumed either way — the broker will not honor it again
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "claim failed"))
        self.token = r["token"]
        self._save_token(self.token)
        return self.token

    def pairing_prompt(self) -> str:
        """A paste-able pairing request: one line the app can put behind a share sheet or
        a copy button. The user launching it in their client is the acceptance — carrying
        the code from the app's UI into the session proves the same possession the
        eyeball-match does, and the code stays single-use and short-lived."""
        code = self.begin_pairing()
        return (f"The app '{self.name}' asks to pair with this session's switchboard: "
                f"if I sent this, accept with switchboard_authorize("
                f"pairing_id='{self._pairing_id}', code='{code}'); otherwise deny it.")

    def await_pairing(self, wait: float = 120.0, poll: float = 1.0) -> str:
        """Block until the user authorizes (or denies / times out), returning the token."""
        if not self._pairing_id:
            raise RuntimeError("begin_pairing first")
        deadline = time.time() + wait
        while time.time() < deadline:
            s = self._call(V.PAIR_STATUS, pairing_id=self._pairing_id)
            if s.get("status") == "authorized":
                self.token = s["token"]
                self._save_token(self.token)
                return self.token
            if s.get("status") == "denied":
                raise Denied("pairing was declined")
            time.sleep(poll)
        raise TimeoutError(f"pairing not authorized within {wait}s")

    # -- requests -------------------------------------------------------------------

    def ask(self, request: Any, wait: float = 120.0, urgency: str = "idle") -> Any:
        """Send a request and return the client's result. Raises NotPaired if the app has
        no valid token yet (the first request patches through to a pairing) — unless a
        spawn secret is on hand, which is redeemed silently first. `urgency` is how the
        session should surface the request: 'idle' waits for the turn to end, 'turn' asks
        to be interjected mid-turn."""
        if self.token is None and self._secret:
            self.claim()
        r = self._call(V.ASK, token=self.token, app=self.name, request=request,
                       urgency=urgency)
        if not r.get("ok"):
            if r.get("status") == "unpaired":
                self._pairing_id = r["pairing_id"]
                # An allowlisted app's pairing opens already authorized, so the token is
                # there to be collected — take it and go, rather than raise NotPaired at
                # a caller who was pre-approved precisely so it need not handle this.
                s = self._call(V.PAIR_STATUS, pairing_id=r["pairing_id"])
                if s.get("status") == "authorized":
                    self.token = s["token"]
                    self._save_token(self.token)
                    r = self._call(V.ASK, token=self.token, app=self.name,
                                   request=request, urgency=urgency)
                if not r.get("ok"):
                    raise NotPaired(r.get("code", ""), r.get("pairing_id", ""))
            else:
                raise RuntimeError(r.get("error", "ask failed"))
        rid = r["request_id"]
        # Not retried if this raises: the request is already queued, and a daemon that
        # died took the queue with it. Re-asking is the app's call, since only the app
        # knows whether its request is safe to send twice.
        res = self._call(V.AWAIT_RESULT, request_id=rid, wait=wait, timeout=wait + 10.0)
        if res.get("status") == "done":
            return res["result"]
        raise TimeoutError(f"no result for {rid} within {wait}s")

    # -- is anyone listening? ---------------------------------------------------------

    def watcher_seen_ago(self) -> Optional[float]:
        """Seconds since a listener last reported to the channel, or None if none ever
        has. An app cannot see the user's client, but it can see whether anything is
        watching on its behalf — the difference between 'the agent is busy' and 'this
        request will sit here until the user happens to type something'."""
        return self._call(V.QUEUE_STATUS).get("watcher_seen_ago")

    def watched(self, within: float = 30.0) -> bool:
        ago = self.watcher_seen_ago()
        return ago is not None and ago <= within

    def watch_prompt(self) -> str:
        """A paste-able line asking the user to arm a watcher, for an app that finds
        nobody listening. The same shape as `pairing_prompt`: the app cannot reach the
        client, but the user can carry one line across."""
        return (f"switchboard: '{self.name}' is waiting on this session, but nothing is "
                f"watching the channel, so its requests will not reach you until you "
                f"type. Please start the listener and keep it running — in Claude Code, "
                f"the Monitor tool (persistent) with: "
                f"uv run python -m switchboard listen")

    def pair_and_ask(self, request: Any, show_code: Callable[[str], None],
                     pair_wait: float = 120.0, ask_wait: float = 120.0) -> Any:
        """The common case: ensure paired (showing the code via `show_code`), then ask."""
        if not self.token:
            code = self.begin_pairing()
            show_code(code)
            self.await_pairing(wait=pair_wait)
        return self.ask(request, wait=ask_wait)
