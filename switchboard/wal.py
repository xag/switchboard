"""The write-ahead log: every request marked durably before it is dispatched.

Evidence before the claim (the ledger's `write-ahead-before-send`). One JSONL file,
append-only, one event per line: a `request` written the instant it is accepted — before
it can be lost to a crash in flight — and a `result` (or `pair`, `authorize`) appended as
each resolves. fsync on every append so a mark survives a power loss, not just a clean
exit. The log may hold requests that never got a result; that is the point.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import discovery


def append(event: dict, path: Path | None = None) -> None:
    """Append one event and force it to disk before returning."""
    path = path or discovery.WAL
    discovery.ensure_home()
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
