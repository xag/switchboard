"""python -m switchboard <command>

    daemon          Run the broker in the foreground (the hook spawns this detached).
    hook            SessionStart hook: bring the shared daemon up idempotently, then exit.
    hook-stop       Stop hook: hold the agent's stop while app requests are queued.
    hook-post-tool  PostToolUse hook: surface urgency='turn' requests mid-turn.
    hook-prompt     UserPromptSubmit hook: mention waiting requests and pairings.
    mcp             Serve the MCP surface on stdio (pairing + the return path).
    status          Print whether a live switchboard is reachable, and where.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"

    if cmd == "daemon":
        from .daemon import run
        return run()

    if cmd == "hook":
        # Spawn-or-find, say nothing on stdout that the client would parse as a directive,
        # and never fail the session start over the channel being down.
        from .daemon import ensure_running
        try:
            info = ensure_running()
            print(f"switchboard ready on 127.0.0.1:{info['port']}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — a down channel must not block the session
            print(f"switchboard: not started ({e})", file=sys.stderr)
        return 0

    if cmd in ("hook-stop", "hook-post-tool", "hook-prompt"):
        from .hooks import run
        return run(cmd.removeprefix("hook-"))

    if cmd == "mcp":
        from .mcp_server import serve
        return serve()

    if cmd == "status":
        from . import discovery
        info = discovery.alive()
        if info:
            print(json.dumps({"live": True, **info}))
            return 0
        print(json.dumps({"live": False}))
        return 1

    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
