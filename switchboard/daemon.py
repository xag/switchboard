"""The loopback-TCP daemon: the broker core wrapped for same-machine apps.

One shared broker per user, reached over 127.0.0.1. The state machine lives in
`core.Switchboard`; this module is only its transport — an asyncio TCP server that reads one
JSON frame, dispatches it to the core, and writes one back. Three parties meet on the wire:
apps (`pair_request`, `ask`, `await_result`), the user authorizing in the client
(`pending_pairings`, `authorize`, `deny`), and the client servicing requests (`take`,
`deliver`).

Run blocking with `python -m switchboard daemon`; the SessionStart hook spawns that
detached. `ensure_running()` is the idempotent front door: it returns the live endpoint,
starting the daemon only if none answers.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, discovery
from .core import PAIR_TTL, Pending, Req, Switchboard  # re-exported for callers and tests

__all__ = ["Switchboard", "Pending", "Req", "PAIR_TTL", "run", "spawn", "ensure_running"]


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
