"""The channel's invariants, decided against a real in-process daemon.

Each test drives the asyncio broker over the wire — the same JSON frames an app sends — so
what is proven is the wire behavior, not a mock of it. A temp HOME keeps the write-ahead
log and discovery file out of the user's real ~/.switchboard.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from switchboard import daemon, discovery, protocol
from switchboard.client import App, Denied, Stale
from switchboard.protocol import V


@pytest.fixture
def channel(tmp_path, monkeypatch):
    """A live switchboard on an ephemeral port, its state in a temp HOME. Yields the
    endpoint; the discovery file is written so client-side liveness works too."""
    monkeypatch.setattr(discovery, "HOME", tmp_path)
    monkeypatch.setattr(discovery, "DISCOVERY", tmp_path / "switchboard.json")
    monkeypatch.setattr(discovery, "WAL", tmp_path / "wal.jsonl")
    monkeypatch.setattr(discovery, "LOG", tmp_path / "daemon.log")

    loop = asyncio.new_event_loop()
    state: dict = {}
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        board = daemon.Switchboard()
        server = loop.run_until_complete(asyncio.start_server(
            lambda r, w: daemon._serve_conn(board, r, w), "127.0.0.1", 0))
        port = server.sockets[0].getsockname()[1]
        discovery.write({"host": "127.0.0.1", "port": port, "pid": -1,
                         "nonce": board.nonce, "version": "test", "started_at": 0})
        state["ep"] = ("127.0.0.1", port)
        state["board"] = board
        state["server"] = server
        ready.set()
        loop.run_forever()
        # Drain the Proactor's pending accept task so teardown is silent on Windows.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5), "daemon did not start"
    yield state["ep"]

    def _shutdown():
        state["server"].close()
        loop.stop()
    loop.call_soon_threadsafe(_shutdown)
    t.join(timeout=2)


def _authorize(ep, app_name):
    """Pair an app and authorize it (the user's act), returning the token."""
    r = protocol.call(ep, V.PAIR_REQUEST, app=app_name)
    a = protocol.call(ep, V.AUTHORIZE, pairing_id=r["pairing_id"], code=r["code"])
    assert a["ok"], a
    return a["token"], r["pairing_id"], r["code"]


def test_first_request_patches_through_to_pairing(channel):
    r = protocol.call(channel, V.ASK, app="notes", request={"q": 1})
    assert r["ok"] is False and r["status"] == "unpaired"
    assert r["code"] and r["pairing_id"]


def test_code_mismatch_is_refused(channel):
    r = protocol.call(channel, V.PAIR_REQUEST, app="notes")
    bad = protocol.call(channel, V.AUTHORIZE, pairing_id=r["pairing_id"], code="000000")
    assert bad["ok"] is False and "mismatch" in bad["error"]
    good = protocol.call(channel, V.AUTHORIZE, pairing_id=r["pairing_id"], code=r["code"])
    assert good["ok"] and good["token"]


def test_pairing_is_idempotent_while_pending(channel):
    a = protocol.call(channel, V.PAIR_REQUEST, app="notes")
    b = protocol.call(channel, V.PAIR_REQUEST, app="notes")
    assert a["pairing_id"] == b["pairing_id"] and a["code"] == b["code"]


def test_ask_take_deliver_await(channel):
    token, _, _ = _authorize(channel, "notes")
    r = protocol.call(channel, V.ASK, token=token, request={"q": "ping"})
    rid = r["request_id"]
    took = protocol.call(channel, V.TAKE, wait=2)
    assert took["request_id"] == rid and took["request"] == {"q": "ping"}
    protocol.call(channel, V.DELIVER, request_id=rid, result={"a": "pong"})
    res = protocol.call(channel, V.AWAIT_RESULT, request_id=rid, wait=2, timeout=5)
    assert res["status"] == "done" and res["result"] == {"a": "pong"}


def test_write_ahead_records_request_before_result(channel, tmp_path):
    token, _, _ = _authorize(channel, "notes")
    r = protocol.call(channel, V.ASK, token=token, request={"q": "durable?"})
    rid = r["request_id"]
    # The request is on disk the instant it is accepted — before any result exists.
    events = [json.loads(x) for x in (tmp_path / "wal.jsonl").read_text().splitlines()]
    kinds = [(e["event"], e.get("request_id")) for e in events]
    assert ("request", rid) in kinds
    assert ("result", rid) not in kinds  # nothing delivered yet
    protocol.call(channel, V.DELIVER, request_id=rid, result="ok")
    events = [json.loads(x) for x in (tmp_path / "wal.jsonl").read_text().splitlines()]
    order = [e["event"] for e in events if e.get("request_id") == rid]
    assert order == ["request", "result"]  # request written strictly before result


def test_bogus_token_is_not_serviced(channel):
    # An invalid token with a name re-opens pairing; with no name it is a plain refusal.
    named = protocol.call(channel, V.ASK, token="nope", app="notes", request={"q": 1})
    assert named["ok"] is False and named["status"] == "unpaired"
    bare = protocol.call(channel, V.ASK, token="nope", request={"q": 1})
    assert bare["ok"] is False and "request_id" not in bare


def test_await_unknown_request_errors(channel):
    r = protocol.call(channel, V.AWAIT_RESULT, request_id="nope", wait=1, timeout=3)
    assert r["ok"] is False


def test_deny_blocks_authorization(channel):
    r = protocol.call(channel, V.PAIR_REQUEST, app="notes")
    protocol.call(channel, V.DENY, pairing_id=r["pairing_id"])
    after = protocol.call(channel, V.AUTHORIZE, pairing_id=r["pairing_id"], code=r["code"])
    assert after["ok"] is False


# -- client library over the same wire ------------------------------------------------

def test_client_pair_and_ask(channel):
    app = App("capital-quiz")
    assert app.stale is False
    code = app.begin_pairing()
    # The user authorizes (here, over the wire) using the code the app showed.
    protocol.call(channel, V.AUTHORIZE, pairing_id=app._pairing_id, code=code)
    token = app.await_pairing(wait=3)
    assert token

    out = {}
    th = threading.Thread(target=lambda: out.__setitem__("r", app.ask({"q": 2}, wait=5)))
    th.start()
    time.sleep(0.2)
    took = protocol.call(channel, V.TAKE, wait=2)
    protocol.call(channel, V.DELIVER, request_id=took["request_id"], result={"a": 4})
    th.join(timeout=5)
    assert out["r"] == {"a": 4}


def test_client_denied_pairing_raises(channel):
    app = App("nosy")
    app.begin_pairing()
    protocol.call(channel, V.DENY, pairing_id=app._pairing_id)
    with pytest.raises(Denied):
        app.await_pairing(wait=2)


def test_client_is_stale_without_a_daemon(tmp_path, monkeypatch):
    # Point discovery at an empty temp HOME: no daemon, so any app is stale.
    monkeypatch.setattr(discovery, "HOME", tmp_path)
    monkeypatch.setattr(discovery, "DISCOVERY", tmp_path / "switchboard.json")
    assert App("orphan").stale is True
    with pytest.raises(Stale):
        App("orphan").begin_pairing()
