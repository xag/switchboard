"""The hook decisions: when a waiting request holds a stop, interjects, or rides a prompt.

The decision functions are pure — hook input and queue status in, directive out — so they
are proven directly; `queue_status` itself is proven against the live channel fixture in
test_switchboard's world (here, with none, it degrades to None and every decision to
silence, which is the invariant that matters most: a down channel never costs a turn).
"""

from __future__ import annotations

from switchboard.hooks import (post_tool_context, prompt_context, queue_status,
                               stop_decision)


def _status(queued=0, interject=0, pairings=0, apps=()):
    return {"ok": True, "queued": queued, "interject": interject,
            "pairings": pairings, "apps": list(apps)}


# -- stop: hold the idle moment while requests wait -----------------------------------

def test_stop_is_blocked_while_requests_wait():
    d = stop_decision({}, _status(queued=2, apps=["notes"]))
    assert d["decision"] == "block"
    assert "notes" in d["reason"] and "switchboard_take" in d["reason"]


def test_stop_passes_when_queue_is_empty():
    assert stop_decision({}, _status()) is None


def test_stop_blocks_only_once_per_cycle():
    # stop_hook_active means we already held this stop — a request the agent cannot
    # service must not trap it in the session forever.
    assert stop_decision({"stop_hook_active": True}, _status(queued=2)) is None


def test_pairings_alone_do_not_hold_a_stop():
    # A pairing needs the user, not the agent; blocking would hold the turn hostage.
    assert stop_decision({}, _status(pairings=3)) is None


# -- mid-turn: only what asked to interject -------------------------------------------

def test_post_tool_surfaces_only_turn_urgency():
    assert post_tool_context(_status(queued=3, interject=0)) is None
    out = post_tool_context(_status(queued=3, interject=1))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "urgency='turn'" in out["hookSpecificOutput"]["additionalContext"]


# -- the user's prompt: mention anything waiting --------------------------------------

def test_prompt_mentions_requests_and_pairings():
    out = prompt_context(_status(queued=1, pairings=1, apps=["notes"]))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "notes" in ctx and "switchboard_pairings" in ctx


def test_prompt_is_silent_when_nothing_waits():
    assert prompt_context(_status()) is None


# -- a replaced channel is reported, never papered over -------------------------------

def test_take_reports_a_replaced_daemon_instead_of_an_empty_queue(monkeypatch):
    """The bug this earns its keep against: a daemon dies holding queued requests, the
    surface silently starts a fresh one, and `take` answers `empty` — which the session
    reads as 'nothing was waiting' one line after a hook said something was."""
    from switchboard import discovery, mcp_server

    live = {"host": "127.0.0.1", "port": 1, "nonce": "first"}
    monkeypatch.setattr(discovery, "alive", lambda *a, **k: live)
    monkeypatch.setattr(mcp_server, "_last_nonce", None, raising=False)

    # First contact establishes which switchboard this surface is speaking to.
    assert mcp_server._endpoint() == ("127.0.0.1", 1)

    # The daemon dies and a replacement takes its place — a different nonce.
    live["nonce"] = "second"
    out = mcp_server._WireHandlers().take()
    assert out["ok"] is False and out["channel"] == "replaced"
    assert "queued requests went with it" in out["error"]


def test_a_down_daemon_is_an_error_not_a_silent_empty(monkeypatch):
    from switchboard import mcp_server

    monkeypatch.setattr(mcp_server, "_endpoint", lambda: ("127.0.0.1", 9))
    monkeypatch.setattr(mcp_server.protocol, "call",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    out = mcp_server._WireHandlers().take()
    assert out["ok"] is False and out["channel"] == "down"


# -- a down channel is silence, never an error ----------------------------------------

def test_no_daemon_means_no_output(tmp_path, monkeypatch):
    from switchboard import discovery
    monkeypatch.setattr(discovery, "HOME", tmp_path)
    monkeypatch.setattr(discovery, "DISCOVERY", tmp_path / "switchboard.json")
    status = queue_status(timeout=0.2)
    assert status is None
    assert stop_decision({}, status) is None
    assert post_tool_context(status) is None
    assert prompt_context(status) is None
