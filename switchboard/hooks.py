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
import os
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


def session_start_context(live: bool) -> Optional[dict]:
    """Ask the agent, once at session start, to arm the listener.

    The hook cannot arm it: a hook runs and exits, a process it spawns detached writes its
    stdout where nobody reads, and no hook directive registers a watcher. But a hook can
    inject context, and the agent CAN arm it — so the last step that must belong to the
    client is at least requested automatically, instead of waiting to be remembered.

    Silent when the channel is down: there is nothing to listen to, and a session start
    must never be spent on advice about a dead channel."""
    if not live:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext":
            "switchboard: the channel is up. The Stop/PostToolUse/UserPromptSubmit hooks "
            "surface app requests while you are working, but no hook fires while you sit "
            "idle at the prompt - so a request sent then waits until the user types. To "
            "close that gap, arm the listener early in this session: run "
            "`uv run python -m switchboard listen` under whatever your client offers for "
            "streaming a background process's stdout (in Claude Code, the Monitor tool, "
            "persistent). Each line it prints then reaches you even while idle. It only "
            "announces - service each request with switchboard_take, then "
            "switchboard_deliver."}}


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
            # watching=True is what separates a listener from the hooks, which poll the
            # same verb but cannot reach an idle session.
            status = protocol.call(discovery.endpoint_of(info), V.QUEUE_STATUS,
                                   timeout=2.0, watching=True)
        except OSError:
            time.sleep(poll)
            continue
        _announce(status, seen)
        time.sleep(poll)


def _announce(status: dict, seen: set) -> None:
    """Print a line for each request not yet announced. Shared by both listeners, so a
    local daemon and a hosted channel say the same thing in the same words.

    ASCII only: these lines cross a console pipe, and a cp1252 console mangles anything
    prettier (the same lesson ledger/check.py records)."""
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
        return
    for req in waiting:
        rid = req["request_id"]
        if rid in seen:
            continue
        seen.add(rid)
        print(f"switchboard: {req['app']} sent request {rid} "
              f"(urgency={req['urgency']}) - service it with switchboard_take, "
              f"then switchboard_deliver.", flush=True)


def listen_remote(url: str, poll: float = 2.0, token: Optional[str] = None) -> int:
    """The listener for an embedded channel: poll a hosted app's own MCP surface.

    A hosted app has no daemon and no discovery file — its channel lives in its own
    process, reachable only as the MCP endpoint the user added as a connector. So this
    watcher speaks MCP rather than the loopback wire, calling `switchboard_waiting`, which
    reports what is queued without taking it. Same announcements, same never-consumes
    promise; only the transport differs, which is the whole point of a transport-free core.

    A guarded channel needs `token` (or SWITCHBOARD_TOKEN): a watcher that cannot
    authenticate is locked out of exactly the channels worth protecting.

    The session is reconnected to on failure and `seen` is cleared when that happens: a
    restarted app's request ids start over, and re-announcing something still queued is a
    far cheaper error than going silent about it."""
    import asyncio

    secret = token or os.environ.get("SWITCHBOARD_TOKEN")

    async def watch() -> None:
        import httpx
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        while True:
            seen: set[str] = set()
            try:
                async with httpx.AsyncClient(headers=headers) as http_client:
                    async with streamable_http_client(
                            url, http_client=http_client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            while True:
                                res = await session.call_tool("switchboard_waiting", {})
                                status = res.structuredContent or {}
                                if status.get("ok"):
                                    _announce(status, seen)
                                await asyncio.sleep(poll)
            except Exception:  # noqa: BLE001 — a hosted app restarts; keep watching
                await asyncio.sleep(poll)

    asyncio.run(watch())
    return 0


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
