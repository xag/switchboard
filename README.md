# switchboard

**[Experimental]** A channel for an app to borrow the user's live client session. The app
sends a request; the session the user is already in services it — reasoning, using its own
tools and context, not just completing a prompt — and the result comes back. One app paired
at a time, every request recorded before it is serviced. It runs two ways: a shared local
daemon for same-machine apps, or embedded in a hosted app that self-hosts its own channel
over remote MCP.

Three parties: the user steers the app from its own UI and from the client, and the app
offloads work to that client. switchboard is the broker between them. It is client-agnostic
— reached only through **MCP and hooks**, spawning nothing — and it carries payloads
faithfully and keeps a record; it does not judge what an app sends.

## The shape

- **One shared daemon per user**, brought up by a `SessionStart` hook. It binds a loopback
  TCP port and publishes it in `~/.switchboard/switchboard.json`; any app and any session
  find it there.
- **An app pairs once**, one of three ways. The default ceremony: the app shows a code,
  the user authorizes it in their client (naming the app), and the code matched on both
  sides confirms it. An app **the session spawns itself** skips the ceremony — the session
  mints a spawn secret (`switchboard_preauthorize`), hands it over in `SWITCHBOARD_SECRET`,
  and the app redeems it once; launching it was the consent. An **external app** folds the
  ceremony into a share: `pairing_prompt()` returns a paste-able line carrying the code,
  and the user launching it in their session is the acceptance. After any of the three,
  requests flow without re-asking.
- **The user's session services requests** by calling the switchboard's MCP tools —
  `switchboard_take` pulls the next request, `switchboard_deliver` returns the result.
- **Waiting requests nudge the agent** two ways, because one is not enough. The client's
  own lifecycle hooks cover an *active* session: a stop is held (once) while the queue is
  non-empty, a request sent with `urgency="turn"` is surfaced mid-turn, and anything
  waiting rides in with the user's next prompt. But a hook only fires on an event the
  client already generates, so a session parked at the prompt hears nothing — for that,
  **`python -m switchboard listen`** prints one line per queued request and the client
  watches its stdout (in Claude Code, the `Monitor` tool). It announces and never
  consumes: `take` stays the agent's act. The channel still spawns nothing and drives no
  session.
- **Liveness is a fact.** If the daemon dies, an app sees itself go `stale`.

## Use it

Install (Python ≥ 3.11): `uv sync`.

**1. Wire the hooks** (shipped in `.claude/settings.json`): `SessionStart` brings the
daemon up idempotently, and `Stop` / `PostToolUse` / `UserPromptSubmit` surface waiting
requests to the agent — each is one cheap frame to the daemon, and silence if it is down:

```json
{ "hooks": {
  "SessionStart":     [ { "hooks": [ { "type": "command", "command": "uv run python -m switchboard hook" } ] } ],
  "Stop":             [ { "hooks": [ { "type": "command", "command": "uv run python -m switchboard hook-stop" } ] } ],
  "PostToolUse":      [ { "hooks": [ { "type": "command", "command": "uv run python -m switchboard hook-post-tool" } ] } ],
  "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "uv run python -m switchboard hook-prompt" } ] } ] } }
```

**2. Mount the MCP surface** — shipped in `.mcp.json`:

```json
{ "mcpServers": { "switchboard": { "command": "uv", "args": ["run", "python", "-m", "switchboard", "mcp"] } } }
```

**3. From an app**, reach the channel:

```python
from switchboard.client import App

app = App("my-app")
answer = app.pair_and_ask({"question": "..."}, show_code=lambda c: print("pairing code:", c))
```

If the session spawned the app with a spawn secret, `App("my-app")` finds it in
`SWITCHBOARD_SECRET` and pairs silently on the first `ask`. An app pairing from the
outside instead offers `app.pairing_prompt()` — one line the user pastes into their
session to accept. `ask(..., urgency="turn")` asks to be surfaced mid-turn rather than
at the next idle moment.

Any language can speak the wire directly — see `switchboard/protocol.py` (newline-delimited
JSON over the loopback port).

Every supported workflow has a runnable demo under [`demos/`](demos/) — the three pairing
paths, urgency, and the embedded hosted channel.

## Embed it in a hosted app

The loopback daemon only reaches apps on the user's machine. A hosted app on a remote server
instead **embeds** the broker: it holds a `Channel` in its own process and serves the
switchboard MCP surface over HTTP. The user adds the app's URL as a remote MCP connector once
— that, plus the same pairing handshake, is the consent. No local daemon, no relay we run.

```python
from switchboard.embed import Channel, NotPaired

channel = Channel("my-hosted-app", record=my_wal_sink)   # the app's own write-ahead store

# Mount the user-side surface in the app's ASGI server (Starlette / FastAPI):
app.mount("/switchboard", channel.mcp_app())   # its /mcp endpoint is the connector URL

# From an app request handler, offload to the user's live session:
async def handle():
    try:
        return await channel.ask({"question": "..."})
    except NotPaired as np:
        show_the_user(np.code)   # the user matches it in their client to authorize once
```

An app that already serves its own MCP surface can skip the separate mount and put the five
tools straight on it with `channel.register_on(mcp)` — then the client that spawned the app
services requests over the same connection, no second connector. This is the shape that
replaces MCP sampling on the app's own surface.

It is the same broker core and the same five tools as the daemon — only the faces differ:
the app reaches the core in-process (`ask`), the user's client reaches it over the network.
Still write-ahead, still transports-not-adjudicates.

## The record

Two day-one artifacts: `switchboard/boundary.py` (the nondeterminism boundary, recorded by
flight-recorder) and `ledger/` (the design decisions as checkable data — `uv run --group
ledger python -m ledger.check`). Tests: `uv run pytest`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). © 2026 Xavier Grehant
