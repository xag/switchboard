# switchboard

**[Experimental]** A shared local channel that connects an app to a live AI-client
session. The app sends a request; the user's own client (Claude Code) services it; the
result comes back — no extra user turn, one app paired at a time, every request recorded
before it is sent.

It is a three-party interaction: the user steers the app from both its own UI and the
client, and the app offloads work to that live client. The switchboard is the broker in
the middle. It carries payloads faithfully and keeps a record of what crossed; it does not
judge what an app sends.

> Experimental because the "service a request with no extra user turn" step leans on
> undocumented Claude Code stream-json behavior that may change without notice. The parts
> that don't — pairing, liveness, the write-ahead record, the return-path tools — are
> solid and tested.

## The shape

- **One shared daemon per user**, brought up idempotently by a `SessionStart` hook. It
  binds a loopback TCP port and publishes it in `~/.switchboard/switchboard.json`; any app
  and any session find it there.
- **An app pairs once.** Its first request patches through to a pairing: the app shows a
  code, the user authorizes it in their client (naming the app), and the code matched on
  both sides confirms it is the right app. After that, requests flow without re-asking.
- **The client services requests** by calling the switchboard's MCP tools — the return
  path. `switchboard_take` pulls the next request; the client answers; `switchboard_deliver`
  sends the result back to the waiting app.
- **Liveness is a fact, not a guess.** If the daemon dies, an app sees itself go `stale`
  rather than hang on a dead channel.

## Use it

Install (Python ≥ 3.11):

```bash
uv sync            # or: pip install -e .
```

**1. Bring the daemon up at session start.** Add a `SessionStart` hook to the project (or
your global `~/.claude/settings.json`). This repo ships one in `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "uv run python -m switchboard hook" } ] }
    ]
  }
}
```

The hook spawns the shared daemon detached and exits; a second session finds it already up.

**2. Mount the MCP surface in the client.** This repo ships `.mcp.json`:

```json
{ "mcpServers": { "switchboard": { "command": "uv", "args": ["run", "python", "-m", "switchboard", "mcp"] } } }
```

Now the client has `switchboard_pairings`, `switchboard_authorize`, `switchboard_take`,
`switchboard_deliver`.

**3. From an app**, reach the channel with the client library:

```python
from switchboard.client import App

app = App("my-app")
# First call pairs: shows a code the user matches in their client, then asks.
answer = app.pair_and_ask({"question": "..."}, show_code=lambda c: print("pairing code:", c))
```

or drive it by hand — `app.begin_pairing()` → show the code → `app.await_pairing()` →
`app.ask(request)`. Any language can speak the wire directly; see `switchboard/protocol.py`
(newline-delimited JSON over the loopback port).

## What v0 does, and doesn't

**Does:** pairing with a both-sides code match, one shared broker with idempotent spawn and
liveness, write-ahead recording of every request before dispatch, and `ask(request) →
result` end to end — verified against a real Claude Code stream-json turn calling the
return-path tools.

**Doesn't yet:** service a request inside an *attended* session with no user-initiated
turn (the injection question — the open bet, tracked in the ledger); mirror mid-turn
requests into session history, so they are lossy on `--resume` (Claude Code #41230); record
the daemon's own socket/stream boundary (only the MCP surface is taped in v0). The design
ledger names each of these as a hypothesis or debt with the condition that discharges it.

## The record

Two day-one artifacts, per the estate's practice:

- **`switchboard/boundary.py`** — the nondeterminism boundary, declared and recorded from
  the first commit (`flight-recorder`).
- **`ledger/`** — the design decisions as checkable data. `uv run --group ledger python -m
  ledger.check` goes red on a decision with no rejected alternative, a belief nothing could
  kill, or a debt with no discharge.

Tests: `uv run pytest`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

© 2026 Xavier Grehant
