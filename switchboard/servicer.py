"""The servicer: a live Claude Code session the switchboard drives.

This is the piece that makes "no extra user turn" real. It runs `claude` in persistent
stream-json mode with the switchboard MCP mounted, and injects a servicing turn whenever
the daemon reports queued work and the session is idle. The human lifts no finger: a queued
request starts a turn that takes it, answers, and delivers it. The session is live and
persistent — it holds context across requests and ends only when stopped, not after one
turn (that is the difference from a headless `-p` one-shot).

    python -m switchboard servicer

The loop is: wait for the daemon to report pending work (a peek, not a take) -> wait until
the session is idle -> inject the servicing turn -> wait for it to finish -> repeat. The
turn itself calls switchboard_take / switchboard_deliver; the servicer only decides *when*
a turn should run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import daemon, discovery, protocol
from .protocol import V

REPO = Path(__file__).resolve().parent.parent

SERVICE_PROMPT = (
    "A paired app has sent work through the switchboard. Loop: call switchboard_take; if "
    "it returns a request, read the request payload, answer it, and call switchboard_deliver "
    "with that request_id and your answer as the result; repeat until switchboard_take "
    "returns {\"empty\": true}. Then stop. Do only this."
)

_ALLOWED = ("mcp__switchboard__switchboard_take,"
            "mcp__switchboard__switchboard_deliver")


def _log(line: str) -> None:
    try:
        discovery.ensure_home()
        with open(discovery.HOME / "servicer.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass
    print(f"[servicer] {line}", file=sys.stderr, flush=True)


class Servicer:
    """A live claude session plus the injector that drives it. Threadless: the reader runs
    on a background thread only to track turn state; everything else is a plain loop."""

    def __init__(self, mcp_config: Optional[str] = None, allowed: str = _ALLOWED) -> None:
        self.mcp_config = str(mcp_config or (REPO / ".mcp.json"))
        self.allowed = allowed
        self.proc: Optional[subprocess.Popen] = None
        self.active = False   # a turn is in flight
        self.ended = False    # the session process has exited

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["claude", "-p", "--input-format", "stream-json", "--output-format",
             "stream-json", "--verbose", "--mcp-config", self.mcp_config,
             "--allowedTools", self.allowed],
            cwd=str(REPO), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        import threading
        threading.Thread(target=self._read, daemon=True).start()
        _log("live session started")

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        _log(f"turn -> {b.get('name')} "
                             f"{json.dumps(b.get('input'))[:140]}")
            elif t == "result":
                self.active = False
        self.ended = True
        _log("session ended (stdout closed)")

    def inject(self, text: str = SERVICE_PROMPT) -> None:
        assert self.proc and self.proc.stdin
        frame = {"type": "user", "message": {"role": "user", "content": text}}
        self.active = True
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        _log("injected a servicing turn")

    def wait_idle(self, timeout: float = 300.0) -> None:
        end = time.time() + timeout
        while self.active and not self.ended and time.time() < end:
            time.sleep(0.15)

    def run(self, poll_wait: float = 25.0) -> int:
        """Bring the channel up, start the live session, and service on demand until the
        session ends."""
        info = discovery.alive() or daemon.ensure_running()
        ep = discovery.endpoint_of(info)
        self.start()
        self.wait_idle(timeout=60)  # let the session finish its init turn, if any
        while not self.ended:
            try:
                r = protocol.call(ep, V.WAIT_PENDING, wait=poll_wait,
                                  timeout=poll_wait + 5)
            except OSError:
                _log("daemon unreachable; stopping")
                break
            if not r.get("pending"):
                continue
            self.wait_idle()
            if self.ended:
                break
            self.inject()
            self.wait_idle()
        return 0

    def stop(self) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def serve() -> int:
    return Servicer().run()
