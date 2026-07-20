"""Installing the hooks into a client's settings: additive, idempotent, reversible.

These act on a temp settings file, never the user's real ~/.claude — the one thing this
module must never get wrong is clobbering a config that holds far more than our entries.
"""

from __future__ import annotations

import json

import pytest

from switchboard.install import (HOOK_EVENTS, hook_command, install, plan,
                                 settings_path, uninstall)


@pytest.fixture
def settings(tmp_path):
    return tmp_path / ".claude" / "settings.json"


def _commands(path):
    hooks = json.loads(path.read_text("utf-8"))["hooks"]
    return {event: [h["command"] for g in hooks.get(event, []) for h in g["hooks"]]
            for event in HOOK_EVENTS}


def test_install_writes_all_four_hooks(settings):
    changes = install(settings)
    assert {a for _, a, _ in changes} == {"add"}
    got = _commands(settings)
    for event, verb in HOOK_EVENTS.items():
        assert any(c.endswith(verb) for c in got[event]), event


def test_install_is_idempotent(settings):
    install(settings)
    before = settings.read_text("utf-8")
    changes = install(settings)
    assert {a for _, a, _ in changes} == {"keep"}
    assert settings.read_text("utf-8") == before  # not even rewritten


def test_install_preserves_unrelated_settings_and_hooks(settings):
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-thing"}]}]},
    }), encoding="utf-8")

    install(settings)
    after = json.loads(settings.read_text("utf-8"))
    assert after["model"] == "opus"                      # untouched
    assert "my-own-thing" in _commands(settings)["Stop"]  # their hook survives
    assert any(c.endswith("hook-stop") for c in _commands(settings)["Stop"])


def test_install_updates_a_moved_command(settings):
    install(settings)
    # Simulate a reinstall from a different environment: same verb, stale path.
    data = json.loads(settings.read_text("utf-8"))
    data["hooks"]["Stop"][0]["hooks"][0]["command"] = "/old/venv/switchboard hook-stop"
    settings.write_text(json.dumps(data), encoding="utf-8")

    changes = dict((e, a) for e, a, _ in install(settings))
    assert changes["Stop"] == "update"
    assert _commands(settings)["Stop"] == [hook_command("hook-stop")]


def test_dry_run_writes_nothing(settings):
    changes = install(settings, dry_run=True)
    assert {a for _, a, _ in changes} == {"add"}
    assert not settings.exists()


def test_uninstall_removes_only_ours(settings):
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-thing"}]}]},
    }), encoding="utf-8")
    install(settings)

    removed = uninstall(settings)
    assert set(removed) == set(HOOK_EVENTS)
    after = json.loads(settings.read_text("utf-8"))
    assert after["model"] == "opus"
    assert after["hooks"]["Stop"][0]["hooks"][0]["command"] == "my-own-thing"


def test_uninstall_leaves_no_empty_scaffolding(settings):
    install(settings)
    uninstall(settings)
    after = json.loads(settings.read_text("utf-8"))
    assert "hooks" not in after  # nothing of ours left behind, not even empty shells


def test_a_backup_is_kept_before_modifying(settings):
    install(settings)
    original = settings.read_text("utf-8")
    uninstall(settings)
    assert settings.with_suffix(".json.bak").read_text("utf-8") == original


def test_a_bom_is_not_mistaken_for_a_broken_config(settings):
    # Windows editors and PowerShell write JSON with a BOM; it is valid settings and must
    # install cleanly, not be reported as malformed.
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8-sig")
    install(settings)
    after = json.loads(settings.read_text("utf-8-sig"))
    assert after["model"] == "opus"
    assert any(c.endswith("hook-stop") for c in _commands(settings)["Stop"])


def test_malformed_settings_are_refused_not_overwritten(settings):
    settings.parent.mkdir(parents=True)
    settings.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        install(settings)
    assert settings.read_text("utf-8") == "{ not json"  # left exactly as found


def test_the_hook_command_is_absolute(tmp_path):
    # A hook fires in whatever directory the session started in, so a relative command
    # (the old `uv run python -m switchboard`) only worked inside this repo.
    for verb in HOOK_EVENTS.values():
        command = hook_command(verb)
        assert command.endswith(verb)
        assert command.startswith('"')  # a quoted absolute path, not a bare word


def test_project_target_is_the_projects_own_settings(tmp_path):
    assert settings_path(project=tmp_path) == tmp_path / ".claude" / "settings.json"
