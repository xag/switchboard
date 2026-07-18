"""The discovery file: how any app and any session find the one live switchboard.

A single shared broker per user (see the ledger) means its endpoint cannot be a fixed
port — it binds an ephemeral one and publishes where it landed. The discovery file at
`~/.switchboard/switchboard.json` carries the endpoint, the pid, and a nonce minted at
start. Liveness is a `ping` that returns the same nonce: a stale file left by an unclean
death fails the check (the port is dead, or reused by an unrelated process whose nonce
differs), so callers never mistake a corpse for the channel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import protocol
from .protocol import Endpoint

HOME = Path.home() / ".switchboard"
DISCOVERY = HOME / "switchboard.json"
WAL = HOME / "wal.jsonl"
LOG = HOME / "daemon.log"


def ensure_home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME


def read() -> Optional[dict]:
    try:
        return json.loads(DISCOVERY.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write(info: dict) -> None:
    """Publish atomically: write a temp file and replace, so a reader never sees half a
    JSON object."""
    ensure_home()
    tmp = DISCOVERY.with_name(DISCOVERY.name + ".tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    tmp.replace(DISCOVERY)


def clear(only_nonce: str | None = None) -> None:
    """Remove the discovery file. If `only_nonce` is given, remove it only when it still
    names our nonce — so a daemon shutting down never clobbers a successor's file."""
    info = read()
    if info is None:
        return
    if only_nonce is not None and info.get("nonce") != only_nonce:
        return
    DISCOVERY.unlink(missing_ok=True)


def endpoint_of(info: dict) -> Endpoint:
    return (info.get("host", "127.0.0.1"), int(info["port"]))


def alive(info: dict | None = None, timeout: float = 2.0) -> Optional[dict]:
    """Return the discovery info if the daemon it names answers `ping` with a matching
    nonce, else None. The one liveness fact everyone reads."""
    info = info if info is not None else read()
    if not info or "port" not in info:
        return None
    try:
        pong = protocol.call(endpoint_of(info), protocol.V.PING, timeout=timeout)
    except OSError:
        return None
    if pong.get("ok") and pong.get("nonce") == info.get("nonce"):
        return info
    return None
