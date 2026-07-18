"""The nondeterminism boundary, declared day one and recorded from the first commit.

The estate's standing practice: name the boundary as the project's first artifact and
record from commit one — never debug by re-deriving what must have happened; replay the
tape and read the variable. A switchboard is almost nothing *but* boundary: every
interesting thing it does is I/O with a party it does not control.

- **the app socket** — requests arrive from apps over loopback TCP, results go back. The
  content is opaque (the channel transports, it does not adjudicate); the framing is ours.
- **the client stream** — the request is serviced by a live AI-client session over
  Claude Code's stream-json; the model's reply is the one genuinely nondeterministic
  input, the same request yielding different results per run.
- **the clock** — timestamps on the write-ahead log, liveness deadlines, pairing-code
  expiry.
- **the pairing randomness** — the code shown on both sides, and the discovery-file nonce.
- **process spawning** — the SessionStart hook brings the daemon up; the daemon may hold
  the client stream.

`install_mcp(boundary(), server)` records every MCP tool call — pairing and the return
path — as tapes under `flight/`. What it does not yet capture is the daemon's own
socket/stream boundary: the daemon is a different process from the MCP server, so recording
its accept/relay path is its own chain, journaled as the `only-the-mcp-surface-is-recorded`
debt and the first refinement this project will file on itself.
"""

from __future__ import annotations

from flight_recorder import Boundary


def boundary() -> Boundary:
    """switchboard's boundary. In v0 the shims are declared by intent and the MCP surface
    is recorded via install_mcp; the daemon-side socket/stream chain is the named next
    refinement (see the ledger). Returned empty so install_mcp records the tool-call
    stream itself — the honest account of one channel's pairings and deliveries."""
    return Boundary()
