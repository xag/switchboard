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
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, discovery
from .core import PAIR_TTL, Pending, Req, Switchboard  # re-exported for callers and tests

__all__ = ["Switchboard", "Pending", "Req", "PAIR_TTL", "run", "spawn", "ensure_running",
           "popen_detached"]


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

# Windows creation flags. CREATE_NO_WINDOW rather than DETACHED_PROCESS: both spare the
# child a console, but DETACHED_PROCESS lets a console *appear* when the launcher is
# itself a console exe (`uv run` shows a black terminal that way). CREATE_BREAKAWAY_FROM_JOB
# is what actually makes "outlives its spawner" true: a process spawned by a member of a
# Windows job object joins that job, and a job with KILL_ON_JOB_CLOSE takes its members
# down when the spawner dies — DETACHED_PROCESS does not escape a job, only breakaway does.
_NO_WINDOW = 0x08000000          # subprocess.CREATE_NO_WINDOW
_NEW_GROUP = 0x00000200          # subprocess.CREATE_NEW_PROCESS_GROUP
_BREAKAWAY = 0x01000000          # subprocess.CREATE_BREAKAWAY_FROM_JOB


def popen_detached(args: list[str], **extra: Any) -> subprocess.Popen:
    """Launch a process that outlives this one and shows no console.

    Breakaway is attempted first and retried without it on failure: a job that permits
    neither explicit nor silent breakaway fails CreateProcess with ACCESS_DENIED, and a
    child inside the job still beats no child at all."""
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL, "close_fds": True,
                          "cwd": str(Path.home()), **extra}
    if os.name != "nt":
        kw["start_new_session"] = True
        return subprocess.Popen(args, **kw)
    try:
        return subprocess.Popen(
            args, creationflags=_NO_WINDOW | _NEW_GROUP | _BREAKAWAY, **kw)
    except OSError:
        return subprocess.Popen(args, creationflags=_NO_WINDOW | _NEW_GROUP, **kw)


def spawn() -> None:
    """Start the daemon as a background process that outlives its spawner.

    stdio to DEVNULL so a full pipe never blocks the child (the hook gotcha); the daemon
    writes its own log to ~/.switchboard/daemon.log. Launched through `uv run` when uv is
    on PATH: in a uv venv `sys.executable` is a trampoline that re-executes the base
    interpreter, and a trampoline whose parent dies mid-launch dies with it."""
    discovery.ensure_home()
    uv = shutil.which("uv")
    root = Path(__file__).resolve().parents[1]
    args = ([uv, "run", "--project", str(root), "python", "-m", "switchboard", "daemon"]
            if uv else [sys.executable, "-m", "switchboard", "daemon"])
    popen_detached(args)


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
