"""The client-side hooks: how a waiting request reaches the agent's attention.

The daemon is already the background listener — the SessionStart hook brought it up, and
apps queue into it whether or not any session is looking. What was missing is the nudge in
the other direction, and a hook is the only client-agnostic place to stand: the channel
still spawns nothing and drives no session (the ledger's services-in-the-users-existing-
session), it only answers the client's own lifecycle events.

Three events, one cheap `queue_status` frame each:

- **Stop** — the agent is about to go idle. If requests are queued, the stop is blocked
  with a reason naming them, so the turn ends only after the queue is drained. This is the
  'waits for idle' delivery: the request is serviced at the first idle moment, and an agent
  mid-conversation is not interrupted. `stop_hook_active` guards the loop: we block a stop
  once; if the agent stops again regardless, it stops.
- **PostToolUse** — the agent is mid-turn between tool calls. Only requests an app marked
  `urgency='turn'` interject here, as injected context, mid-turn by design.
- **UserPromptSubmit** — the user speaks; anything waiting (requests or pairings) rides in
  as context so the session knows without being asked.

Every function degrades to silence: no daemon, no queue, or any error at all means no
output and exit 0 — the channel being down must never cost the user their turn.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Optional

from . import discovery, protocol
from .protocol import V


def queue_status(timeout: float = 2.0) -> Optional[dict]:
    """The live daemon's queue counts, or None if no daemon answers."""
    info = discovery.alive(timeout=timeout)
    if not info:
        return None
    try:
        status = protocol.call(discovery.endpoint_of(info), V.QUEUE_STATUS,
                               timeout=timeout)
    except OSError:
        return None
    return status if status.get("ok") else None


def _naming(status: dict) -> str:
    apps = status.get("apps") or []
    n = status["queued"]
    who = f" from {', '.join(apps)}" if apps else ""
    return f"{n} app request(s){who}"


def stop_decision(hook_input: dict, status: Optional[dict]) -> Optional[dict]:
    """Block the stop while requests wait — once. A second stop in the same cycle
    (stop_hook_active) passes, so a request the agent cannot service never traps it."""
    if hook_input.get("stop_hook_active"):
        return None
    if not status or status["queued"] == 0:
        return None
    return {"decision": "block",
            "reason": f"switchboard: {_naming(status)} waiting on the channel. Call "
                      f"switchboard_take, service each request, and return results with "
                      f"switchboard_deliver; stop when the queue is empty."}


def post_tool_context(status: Optional[dict]) -> Optional[dict]:
    """Mid-turn, surface only what asked to be surfaced mid-turn."""
    if not status or status["interject"] == 0:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext":
            f"switchboard: {status['interject']} request(s) marked urgency='turn' are "
            f"waiting. When you reach a natural point in this turn, call switchboard_take "
            f"and service them."}}


def prompt_context(status: Optional[dict]) -> Optional[dict]:
    """On the user's prompt, mention anything waiting — requests or pairings."""
    if not status or (status["queued"] == 0 and status["pairings"] == 0):
        return None
    parts = []
    if status["queued"]:
        parts.append(f"{_naming(status)} waiting — service with switchboard_take")
    if status["pairings"]:
        parts.append(f"{status['pairings']} app(s) waiting to pair — see "
                     f"switchboard_pairings")
    return {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "switchboard: " + "; ".join(parts) + "."}}


def listen(poll: float = 1.0) -> int:
    """The listener: one stdout line per app request, for as long as it runs.

    This is the active half of the nudge, and the passive hooks cannot replace it. A hook
    only fires on an event the client already generates — a tool call, a stop, a user
    message — so a session parked at the prompt hears nothing until the user speaks. A
    listener runs outside the turn loop: the client watches its stdout, and a line arriving
    while the session is idle reaches the agent on its own. That is what 'wakes up the
    agent if idle' means, and it is the only thing here that does it.

    It announces; it never consumes. `take` stays the agent's act over MCP, so a request is
    still written ahead, still taken once, and still delivered by the session — the listener
    only says that something is there. Emissions are deduplicated per daemon lifetime; a
    replaced daemon (new nonce) resets that memory, because its request ids start over."""
    seen: set[str] = set()
    nonce: Optional[str] = None
    while True:
        info = discovery.alive(timeout=2.0)
        if info is None:
            # A dead channel is quiet, not chatty: it recovers, and the hooks still cover
            # the events that do fire. Forget nothing — a restart is caught by the nonce.
            time.sleep(poll)
            continue
        if info.get("nonce") != nonce:
            nonce, seen = info.get("nonce"), set()
        try:
            status = protocol.call(discovery.endpoint_of(info), V.QUEUE_STATUS,
                                   timeout=2.0)
        except OSError:
            time.sleep(poll)
            continue
        waiting = status.get("waiting")
        if waiting is None:
            # An older daemon predates `waiting` and reports only counts. The channel is
            # shared and long-lived, so a newer client meeting an older daemon is normal,
            # not an error — announce on the rise from empty rather than go mute. Being
            # vaguer than the identified path beats the silence that hid this bug.
            if status.get("queued", 0) and not seen:
                seen.add("counted")
                print(f"switchboard: {status['queued']} request(s) waiting from "
                      f"{', '.join(status.get('apps') or ['an app'])} - service with "
                      f"switchboard_take, then switchboard_deliver.", flush=True)
            elif not status.get("queued", 0):
                seen.discard("counted")
            time.sleep(poll)
            continue
        for req in waiting:
            rid = req["request_id"]
            if rid in seen:
                continue
            seen.add(rid)
            # ASCII only: this line crosses a console pipe, and a cp1252 console mangles
            # anything prettier (the same lesson ledger/check.py records).
            print(f"switchboard: {req['app']} sent request {rid} "
                  f"(urgency={req['urgency']}) - service it with switchboard_take, "
                  f"then switchboard_deliver.", flush=True)
        time.sleep(poll)


def run(event: str) -> int:
    """Entry for `python -m switchboard hook-<event>`. Reads the hook payload from stdin,
    asks the daemon, prints a JSON directive if there is one. Always exits 0."""
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        hook_input = {}
    try:
        status = queue_status()
        out = {"stop": lambda: stop_decision(hook_input, status),
               "post-tool": lambda: post_tool_context(status),
               "prompt": lambda: prompt_context(status)}[event]()
    except Exception:  # noqa: BLE001 — a broken channel must never fail the hook
        return 0
    if out is not None:
        print(json.dumps(out))
    return 0
