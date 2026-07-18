"""The nondeterminism boundary, declared day one and recorded from the first commit.

The estate's practice: name the boundary as the project's first artifact and record from
commit one, so a bug is replayed, not re-derived. switchboard's boundary is its I/O — the
app socket, the clock (log timestamps, liveness deadlines), and the pairing randomness (the
code and the discovery nonce).

`install_mcp(boundary(), server)` records every MCP tool call — pairing and the return
path — as tapes under `~/.switchboard/flight`.
"""

from __future__ import annotations

from flight_recorder import Boundary


def boundary() -> Boundary:
    """Returned empty of shims so install_mcp records the tool-call stream itself — the
    honest account of a channel's pairings and deliveries."""
    return Boundary()
