"""Apps the user has already admitted — so a pairing is asked once, not every restart.

Pairings used to live only in memory, which meant a daemon restart made every app a
stranger again and re-asked a question the user had already answered. Ceremony that
repeats for an answer already given is the kind users learn to click through, which is
exactly what makes the ceremony worthless when it finally matters.

Two ways in, recorded the same way:

- **paired** — the user matched a code and authorized. Remembered from then on.
- **allowlisted** — named in this file ahead of time (`switchboard allow <app>`), for an
  app the user installed deliberately and does not want to be asked about at all.

What is stored is a SHA-256 of the token, never the token. The app keeps the only copy;
this file lets the daemon recognise it without being able to mint or replay it, so the
registry is a record of decisions and not a pile of credentials.

The honest limit: on a single-user machine every process runs as the user and can read
the app's stored token, so this cannot distinguish "your notes app" from "something that
read your notes app's token file". It is not trying to. It records which apps the user
admitted, so the consent moment happens once — the machine's own boundaries are what keep
processes apart, and switchboard does not pretend to replace them.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from . import discovery


def path() -> Path:
    """Resolved on each call so a relocated HOME (tests, another user) is honoured."""
    return discovery.HOME / "apps.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Cached on (path, mtime, size) so the revocation check on every request costs a stat,
# not a parse. Revocation has to be checked constantly to mean anything, and a check too
# expensive to run constantly is a check that ends up running once at startup.
_cache: tuple = (None, None, None, {})


def load() -> dict:
    global _cache
    target = path()
    try:
        st = target.stat()
        key = (str(target), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _cache[:3] == key:
        return _cache[3]
    try:
        data = json.loads(target.read_text("utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    data = data if isinstance(data, dict) else {}
    _cache = (*key, data)
    return data


def knows(token: str) -> bool:
    """Is this token still vouched for? Asked on every request, so that revoking an app
    takes effect at once rather than at the next daemon restart."""
    return app_for_token(token) is not None


def _save(data: dict) -> None:
    discovery.ensure_home()
    target = path()
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)


def remember(app: str, token: str) -> None:
    """Record that the user admitted `app`, and how to recognise it again."""
    data = load()
    entry = data.get(app) or {}
    entry.update({"token_sha256": _digest(token), "source": entry.get("source", "paired"),
                  "approved_at": entry.get("approved_at", time.time())})
    data[app] = entry
    _save(data)


def app_for_token(token: str) -> Optional[str]:
    """The app this token belongs to, if any — how a remembered pairing is recognised by
    a daemon that has never seen this app in its own lifetime."""
    if not token:
        return None
    wanted = _digest(token)
    for app, entry in load().items():
        if entry.get("token_sha256") == wanted:
            return app
    return None


def is_allowlisted(app: str) -> bool:
    entry = load().get(app)
    return bool(entry and entry.get("source") == "allowlisted")


def allow(app: str) -> None:
    """Pre-approve an app by name, before it has ever connected. This is the config-file
    form of the trust the user already showed by installing it — deliberate, inspectable,
    and revocable in the same place, rather than a prompt answered in the moment."""
    data = load()
    entry = data.get(app) or {}
    entry.update({"source": "allowlisted", "approved_at": entry.get("approved_at",
                                                                   time.time())})
    data[app] = entry
    _save(data)


def forget(app: str) -> bool:
    data = load()
    if app not in data:
        return False
    del data[app]
    _save(data)
    return True


def entries() -> list[dict]:
    return [{"app": app, "source": e.get("source", "paired"),
             "approved_at": e.get("approved_at"), "remembered": "token_sha256" in e}
            for app, e in sorted(load().items())]
