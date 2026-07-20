"""Installing the hooks into a client's settings — explicitly, never as a side effect.

Installing the package puts a library and a CLI on the machine; it does not touch the
user's client configuration, and it must not. Wiring hooks into a session means arranging
for code to run on someone's every prompt and every stop — that is the user's decision to
make out loud, so it is a command they run (`switchboard install-hooks`), not a side effect
of `pip install`. A channel whose whole design spawns nothing does not get to install
itself either.

The command it writes is an absolute path, not `uv run python -m switchboard`. A hook fires
in whatever directory the session was started in, and `uv run` resolves its project from the
current directory — so the repo-relative form only ever worked while the session happened to
sit in this repo. Absolute is what makes the hooks work anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

# Each client event and the subcommand that answers it.
HOOK_EVENTS: dict[str, str] = {
    "SessionStart": "hook",
    "Stop": "hook-stop",
    "PostToolUse": "hook-post-tool",
    "UserPromptSubmit": "hook-prompt",
}


def hook_command(verb: str) -> str:
    """The command a hook entry runs, resolved absolutely.

    Prefer the installed console script; fall back to this interpreter and `-m`. Either
    way it names a full path, so the hook does not depend on which directory the session
    started in or which environment happens to be active."""
    exe = Path(sys.executable)
    script = exe.parent / ("switchboard.exe" if os.name == "nt" else "switchboard")
    if script.exists():
        return f'"{script}" {verb}'
    found = shutil.which("switchboard")
    if found:
        return f'"{found}" {verb}'
    return f'"{exe}" -m switchboard {verb}'


def settings_path(user: bool = True, project: Optional[Path] = None) -> Path:
    """Where the hooks go: the user's client settings, or one project's."""
    if project is not None:
        return Path(project).resolve() / ".claude" / "settings.json"
    if user:
        return Path.home() / ".claude" / "settings.json"
    raise ValueError("name a target: user settings or a project")


def _read(path: Path) -> dict:
    # utf-8-sig, not utf-8: Windows editors and PowerShell write JSON with a BOM, and
    # json.loads chokes on it. Reading a real settings file and calling it malformed is
    # worse than useless — it tells the user their working config is broken. The sig
    # codec strips a BOM when present and is plain utf-8 when it is not; we write without.
    try:
        return json.loads(path.read_text("utf-8-sig"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path} is not valid JSON ({e}) — fix or move it first, "
                           f"rather than have this command overwrite it") from e


def _write(path: Path, settings: dict) -> None:
    """Write atomically, keeping a .bak of what was there. This is the user's own client
    configuration and it holds far more than our four entries — losing it to a crashed
    write, or to a bug of ours, is not an acceptable cost of installing a hook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _is_ours(command: str, verb: str) -> bool:
    """Ours if it names switchboard and ends with this exact verb — so `hook` never
    matches `hook-stop`, and a user's own unrelated hook is never touched."""
    parts = command.strip().split()
    return bool(parts) and parts[-1] == verb and "switchboard" in command


def plan(settings: dict) -> list[tuple[str, str, str]]:
    """What installing would change: (event, action, command). Pure — it writes nothing,
    so `--dry-run` and the real thing cannot disagree."""
    out: list[tuple[str, str, str]] = []
    hooks = settings.get("hooks", {})
    for event, verb in HOOK_EVENTS.items():
        want = hook_command(verb)
        mine = [h for group in hooks.get(event, [])
                for h in group.get("hooks", [])
                if _is_ours(h.get("command", ""), verb)]
        if not mine:
            out.append((event, "add", want))
        elif any(h.get("command") != want for h in mine):
            out.append((event, "update", want))
        else:
            out.append((event, "keep", want))
    return out


def install(path: Path, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Merge our four hooks into `path`, leaving every other setting untouched."""
    settings = _read(path)
    changes = plan(settings)
    if dry_run or all(action == "keep" for _, action, _ in changes):
        return changes
    hooks = settings.setdefault("hooks", {})
    for event, action, want in changes:
        groups = hooks.setdefault(event, [])
        if action == "add":
            groups.append({"hooks": [{"type": "command", "command": want}]})
        elif action == "update":
            for group in groups:
                for h in group.get("hooks", []):
                    if _is_ours(h.get("command", ""), HOOK_EVENTS[event]):
                        h["command"] = want
    _write(path, settings)
    return changes


def uninstall(path: Path, dry_run: bool = False) -> list[str]:
    """Remove only our entries — installing into a shared config must be reversible by
    the same tool that did it, or it is a trap rather than a convenience."""
    settings = _read(path)
    hooks = settings.get("hooks", {})
    removed: list[str] = []
    for event, verb in HOOK_EVENTS.items():
        groups = hooks.get(event)
        if not groups:
            continue
        for group in groups:
            keep = [h for h in group.get("hooks", [])
                    if not _is_ours(h.get("command", ""), verb)]
            if len(keep) != len(group.get("hooks", [])):
                removed.append(event)
            group["hooks"] = keep
        # Drop the shells our removal emptied; leave any the user still fills.
        hooks[event] = [g for g in groups if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    if removed and not dry_run:
        if not hooks:
            settings.pop("hooks", None)
        _write(path, settings)
    return removed
