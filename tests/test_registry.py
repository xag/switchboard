"""Remembering who was admitted: paired once, allowlisted, or revoked.

The point of these is that a decision the user already made is not re-asked. The restart
test is the one that matters — that was the behaviour that made the ceremony repeat.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from switchboard import discovery, registry
from switchboard.core import Switchboard


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "HOME", tmp_path)
    monkeypatch.setattr(discovery, "WAL", tmp_path / "wal.jsonl")
    return tmp_path


def _board():
    return Switchboard(record=lambda e: None)


def _ask(board, msg):
    """`ask` parks a future for the result, so it needs a running loop."""
    async def body():
        return board.ask(msg)
    return asyncio.run(body())


def _pair(board, app="notes"):
    opened = board.pair_request({"app": app})
    granted = board.authorize({"pairing_id": opened["pairing_id"],
                               "code": opened["code"]})
    assert granted["ok"], granted
    return granted["token"]


# -- a pairing outlives the daemon that granted it ------------------------------------

def test_a_remembered_app_is_recognised_by_a_new_daemon():
    token = _pair(_board())
    # The daemon dies; a new one starts knowing nothing in memory.
    fresh = _board()
    assert fresh.by_token == {}
    r = _ask(fresh, {"token": token, "request": {"q": 1}})
    assert r["ok"] and r["request_id"], r


def test_forgetting_an_app_sends_it_back_through_pairing():
    token = _pair(_board())
    assert registry.forget("notes") is True
    fresh = _board()
    r = _ask(fresh, {"token": token, "app": "notes", "request": {"q": 1}})
    assert r["ok"] is False and r["status"] == "unpaired"


def test_revoking_bites_a_running_daemon_not_just_the_next_one():
    """Nearly shipped broken: `forget` removed the registry entry while the live daemon
    kept honouring the token from memory, so revocation did nothing until a restart —
    which is precisely the moment you would not want to wait for."""
    board = _board()
    token = _pair(board)
    assert _ask(board, {"token": token, "request": {"q": 1}})["ok"]

    registry.forget("notes")

    r = _ask(board, {"token": token, "app": "notes", "request": {"q": 2}})
    assert r["ok"] is False and r["status"] == "unpaired"


def test_an_embedded_channel_keeps_out_of_the_local_registry(temp_home):
    """A hosted app may not even run on the user's machine; its pairings are its own."""
    board = Switchboard(record=lambda e: None, remember=False)
    _pair(board, "hosted-app")
    assert not (temp_home / "apps.json").exists()


def test_an_unknown_token_is_still_refused():
    _pair(_board())
    fresh = _board()
    r = _ask(fresh, {"token": "not-a-real-token", "request": {"q": 1}})
    assert r["ok"] is False and "request_id" not in r


# -- the allowlist: pre-approved by name, never asked ---------------------------------

def test_an_allowlisted_app_pairs_without_a_ceremony():
    registry.allow("trusted-notes")
    board = _board()
    opened = board.pair_request({"app": "trusted-notes"})
    # Already authorized: nothing is waiting for the user to approve.
    assert board.pending_pairings()["pairings"] == []
    status = board.pair_status({"pairing_id": opened["pairing_id"]})
    assert status["status"] == "authorized" and status["token"]
    assert _ask(board, {"token": status["token"], "request": {"q": 1}})["ok"]


def test_an_ordinary_app_still_waits_for_the_user():
    board = _board()
    board.pair_request({"app": "stranger"})
    assert [p["app"] for p in board.pending_pairings()["pairings"]] == ["stranger"]


# -- what the registry stores ----------------------------------------------------------

def test_the_registry_stores_a_hash_never_the_token(temp_home):
    token = _pair(_board())
    raw = (temp_home / "apps.json").read_text("utf-8")
    assert token not in raw
    assert json.loads(raw)["notes"]["token_sha256"]


def test_entries_reports_how_each_app_got_in():
    _pair(_board(), "paired-app")
    registry.allow("listed-app")
    by_app = {e["app"]: e for e in registry.entries()}
    assert by_app["paired-app"]["source"] == "paired"
    assert by_app["listed-app"]["source"] == "allowlisted"


def test_a_corrupt_registry_does_not_take_the_channel_down(temp_home):
    (temp_home / "apps.json").write_text("{ not json", encoding="utf-8")
    assert registry.load() == {}
    board = _board()  # still usable: an unreadable registry means nobody is remembered
    r = _ask(board, {"token": "anything", "app": "notes", "request": {}})
    assert r["status"] == "unpaired"
