"""The wire: newline-delimited JSON over a loopback TCP socket.

One frame is one line of UTF-8 JSON terminated by '\\n'. A request carries a `verb` and
its fields; a response carries `ok` plus fields, or `ok: false` with an `error`. The
format is deliberately the plainest thing every language's standard library can speak — an
app in any runtime reaches the channel with a socket and a JSON encoder, nothing more.

`call` is the synchronous client side used by the app library and the MCP surface (both
are clients of the daemon). Long-poll verbs (`take`, `await_result`) simply hold the
connection until the daemon answers, so `timeout` must exceed the daemon-side wait.
"""

from __future__ import annotations

import json
import socket
from typing import Any


class V:
    """The verbs. Kept as bare strings so the wire stays inspectable."""

    PING = "ping"                       # -> {ok, nonce, pid, version}
    PAIR_REQUEST = "pair_request"       # {app} -> {ok, pairing_id, code}
    PENDING_PAIRINGS = "pending_pairings"  # -> {ok, pairings:[{pairing_id, app, code}]}
    PAIR_STATUS = "pair_status"         # {pairing_id} -> {ok, status, token?}
    AUTHORIZE = "authorize"             # {pairing_id, code} -> {ok, token, app} | code mismatch
    DENY = "deny"                       # {pairing_id} -> {ok}
    ASK = "ask"                         # {token, request} -> {ok, request_id} | {ok:false, status:"unpaired", ...}
    AWAIT_RESULT = "await_result"       # {request_id} (long-poll) -> {ok, status, result?}
    TAKE = "take"                       # {} (long-poll) -> {ok, request_id, app, request} | {ok, empty:true}
    DELIVER = "deliver"                 # {request_id, result} -> {ok}


Endpoint = tuple[str, int]


def call(endpoint: Endpoint, verb: str, timeout: float = 10.0, **fields: Any) -> dict:
    """Open a connection, send one frame, read one frame, close. Raises OSError if the
    daemon is unreachable — the caller turns that into liveness (`stale`)."""
    host, port = endpoint
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps({"verb": verb, **fields}) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    text = buf.decode("utf-8").strip()
    if not text:
        return {"ok": False, "error": "empty response"}
    return json.loads(text)
