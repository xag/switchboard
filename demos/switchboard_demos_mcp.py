"""The switchboard-demos MCP server: the demo apps, callable as MCP tools.

This is the "MCP app" shape from issue 4: the session calls a tool, the tool spawns the
app pre-approved — it mints the spawn secret over the wire and hands it to the app in
SWITCHBOARD_SECRET, so the app pairs with no code and no user action. The session
calling the tool is the consent; nothing here may preauthorize an app the session did
not ask for.

Mounted by .mcp.json as `switchboard-demos`:

    { "command": "uv", "args": ["run", "python", "demos/switchboard_demos_mcp.py"] }
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any  # noqa: F401 — tool return annotations

_DEMOS = Path(__file__).resolve().parent
sys.path.insert(0, str(_DEMOS.parent))  # runnable from anywhere, not only the repo root

from mcp.server.fastmcp import FastMCP  # noqa: E402

from switchboard import daemon, discovery, protocol  # noqa: E402  (popen_detached, too)
from switchboard.client import SECRET_ENV  # noqa: E402
from switchboard.protocol import V  # noqa: E402

mcp = FastMCP("switchboard-demos")


def _preauthorize(app: str) -> str:
    """Mint a spawn secret for an app this server is about to spawn — over the same wire
    the switchboard_preauthorize tool uses, with the daemon brought up if need be."""
    info = discovery.alive() or daemon.ensure_running()
    r = protocol.call(discovery.endpoint_of(info), V.PREAUTHORIZE, app=app)
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "preauthorize failed"))
    return r["secret"]


def _spawn(script: str, *args: str, secret: str) -> int:
    """Launch a demo app windowless and outliving this server, secret in its environment.

    Three hard-won details, each paid for by a demo that failed in front of the user:
    launch through `uv run` rather than sys.executable (in a uv venv that is a trampoline
    which re-executes the base interpreter, and a trampoline whose parent dies mid-launch
    dies with it — error 0x800700e8); use switchboard's own `popen_detached`, whose
    CREATE_NO_WINDOW spares the user the black terminal a console launcher would flash and
    whose job breakaway keeps the app alive past this server's lifetime; and log to
    ~/.switchboard/demos.log rather than DEVNULL, because a demo that dies before pairing
    must leave a trace."""
    env = dict(os.environ, **{SECRET_ENV: secret})
    root = _DEMOS.parent
    uv = shutil.which("uv")
    cmd = ([uv, "run", "--project", str(root), "python", str(_DEMOS / script), *args]
           if uv else [sys.executable, str(_DEMOS / script), *args])
    discovery.ensure_home()
    with open(discovery.HOME / "demos.log", "ab") as log:
        return daemon.popen_detached(
            cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(root), env=env).pid


@mcp.tool(structured_output=True)
def demo_button_app() -> dict[str, Any]:
    """Spawn the button-demo window, pre-approved: it pairs silently on the first click
    and each click sends the entered prompt to this session with urgency='turn', so it
    is surfaced mid-turn. The answer you deliver lands back in the window."""
    secret = _preauthorize("button-demo")
    pid = _spawn("button_app.py", secret=secret)
    return {"ok": True, "pid": pid,
            "note": "window is up — the first click pairs silently; service each click "
                    "with switchboard_take / switchboard_deliver"}


@mcp.tool(structured_output=True)
def demo_idle_message(message: str, delay_seconds: float = 0.0) -> dict[str, Any]:
    """Dispatch a messenger that sends `message` through the channel with urgency='idle':
    nothing interrupts the session mid-turn — the message waits for its next idle moment
    (the held stop, or the next prompt), when the session posts it visibly to the user
    and delivers an acknowledgement back. `delay_seconds` delays the send, e.g. to let
    the session go quiet first."""
    secret = _preauthorize("idle-messenger")
    pid = _spawn("idle_messenger.py", str(delay_seconds), message, secret=secret)
    return {"ok": True, "pid": pid,
            "note": "messenger dispatched — the message waits for the session's next "
                    "idle moment, to be shown to the user"}


if __name__ == "__main__":
    mcp.run()
