# switchboard

**[Experimental]** A shared local channel that connects an app to a live AI-client
session. The app sends a request; the user's own client (Claude Code) services it; the
result comes back — no extra user turn, one app paired at a time, every request recorded
before it is sent.

It is a three-party interaction: the user steers the app from both its own UI and the
client, and the app offloads work to that live client. The switchboard is the broker in
the middle. It carries payloads faithfully and keeps a record of what crossed; it does not
judge what an app sends.

> "No extra user turn" means the user lifts no finger: a request arriving while the client
> is idle simply starts a turn carrying it. That servicing step leans on undocumented Claude
> Code stream-json turn injection, which may change without notice — hence experimental. The
> parts that don't — pairing, liveness, the write-ahead record, the return-path tools — are
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
  sends the result back to the waiting app. A live session does this on its own:
  `python -m switchboard servicer` runs a persistent Claude Code session the daemon drives,
  auto-injecting a servicing turn whenever a request is queued — the user lifts no finger.
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

**3. Run a servicing session** — this is the point. It launches a live, persistent Claude
Code session that services queued requests automatically, by injecting a turn when work
arrives:

```bash
uv run python -m switchboard servicer
```

**4. From an app**, reach the channel with the client library:

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
result` end to end — the daemon **auto-injects a servicing turn into a live, persistent
stream-json session** (the servicer), so a queued request is serviced with no user
finger-lift. Verified with two requests answered over one live session, the session keeping
context between them (persistent, not a `-p` one-shot).

**Doesn't yet:** point the servicer at the user's *own* already-running interactive session
— v0's servicer is a live session the daemon launches, which is the mechanism; attaching to
a session the user is already in is next. Mid-turn requests aren't mirrored into session
history, so they are lossy on `--resume` (Claude Code #41230). Only the MCP surface is
taped, not the daemon's own socket/stream boundary. The design ledger names the mid-turn
gap and the recording gap as debts with the condition that discharges each.

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
