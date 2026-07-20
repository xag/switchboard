"""switchboard <command>   (or: python -m switchboard <command>)

    install-hooks   Wire the hooks into a client's settings, so every session has the
                    channel. --user (default) or --project [PATH]; --dry-run to look.
    uninstall-hooks Remove them again from the same place.
    daemon          Run the broker in the foreground (the hook spawns this detached).
    hook            SessionStart hook: bring the shared daemon up idempotently, then exit.
    hook-stop       Stop hook: hold the agent's stop while app requests are queued.
    hook-post-tool  PostToolUse hook: surface urgency='turn' requests mid-turn.
    hook-prompt     UserPromptSubmit hook: mention waiting requests and pairings.
    listen          Announce each app request on stdout, one line apiece, until stopped.
                    Run under a client watcher (Monitor) so a request reaches the agent
                    even while the session sits idle — the hooks cannot do that alone.
                    --url URL watches a hosted app's embedded channel over MCP instead
                    of the local daemon; --token (or SWITCHBOARD_TOKEN) authenticates
                    to a guarded one.
    mcp             Serve the MCP surface on stdio (pairing + the return path).
    status          Print whether a live switchboard is reachable, and where.
"""

from __future__ import annotations

import json
import sys


def _install_cmd(cmd: str, argv: list[str]) -> int:
    """`install-hooks` / `uninstall-hooks`, with the target named explicitly."""
    from pathlib import Path

    from .install import install, settings_path, uninstall

    dry = "--dry-run" in argv
    rest = [a for a in argv if a != "--dry-run"]
    project: Path | None = None
    if "--project" in rest:
        i = rest.index("--project")
        after = rest[i + 1:]
        project = Path(after[0]) if after and not after[0].startswith("-") else Path.cwd()
    try:
        path = settings_path(user=project is None, project=project)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        if cmd == "install-hooks":
            changes = install(path, dry_run=dry)
            for event, action, command in changes:
                print(f"{'would ' if dry and action != 'keep' else ''}{action:6s} "
                      f"{event}: {command}")
            print(f"\n{'(dry run) ' if dry else ''}{path}")
            if not dry:
                print("Hooks active from the next session. Undo with: "
                      "switchboard uninstall-hooks"
                      + (f" --project {project}" if project else ""))
        else:
            removed = uninstall(path, dry_run=dry)
            print(f"{'would remove ' if dry else 'removed '}"
                  f"{', '.join(removed) if removed else 'nothing (none installed)'}")
            print(f"{path}")
    except RuntimeError as e:  # a settings file we must not clobber
        print(str(e), file=sys.stderr)
        return 1
    return 0


def cli() -> int:
    """Console-script entry point (`switchboard ...`), from [project.scripts]."""
    return main(sys.argv[1:])


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"

    if cmd in ("install-hooks", "uninstall-hooks"):
        return _install_cmd(cmd, argv[1:])

    if cmd == "daemon":
        from .daemon import run
        return run()

    if cmd == "hook":
        # Spawn-or-find, then ask the agent to arm the listener — the one step of the
        # wake-on-idle path a hook cannot take itself. The human line goes to stderr;
        # stdout carries only the hook protocol's own JSON. Never fail the session start
        # over the channel being down.
        from .daemon import ensure_running
        from .hooks import session_start_context
        live = False
        try:
            info = ensure_running()
            print(f"switchboard ready on 127.0.0.1:{info['port']}", file=sys.stderr)
            live = True
        except Exception as e:  # noqa: BLE001 — a down channel must not block the session
            print(f"switchboard: not started ({e})", file=sys.stderr)
        out = session_start_context(live)
        if out is not None:
            print(json.dumps(out))
        return 0

    if cmd in ("hook-stop", "hook-post-tool", "hook-prompt"):
        from .hooks import run
        return run(cmd.removeprefix("hook-"))

    if cmd == "listen":
        from .hooks import listen, listen_remote
        url = None
        if "--url" in argv:
            i = argv.index("--url")
            url = argv[i + 1] if len(argv) > i + 1 else None
            if not url:
                print("--url needs the channel's MCP endpoint", file=sys.stderr)
                return 2
        token = None
        if "--token" in argv:
            i = argv.index("--token")
            token = argv[i + 1] if len(argv) > i + 1 else None
        try:
            return listen_remote(url, token=token) if url else listen()
        except KeyboardInterrupt:
            return 0

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
