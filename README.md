# switchboard

**[Experimental]** A channel that connects an app to the user's live client session. The app
sends a request; the session the user is already in services it; the result comes back — one
app paired at a time, every request recorded before it is sent. It runs two ways: a shared
local daemon for same-machine apps, or embedded in a hosted app that self-hosts its own
channel over remote MCP.

Three parties: the user steers the app from its own UI and from the client, and the app
offloads work to that client. switchboard is the broker between them. It is client-agnostic
— reached only through **MCP and hooks**, spawning nothing — and it carries payloads
faithfully and keeps a record; it does not judge what an app sends.

## The shape

- **One shared daemon per user**, brought up by a `SessionStart` hook. It binds a loopback
  TCP port and publishes it in `~/.switchboard/switchboard.json`; any app and any session
  find it there.
- **An app pairs once.** Its first request patches through to a pairing: the app shows a
  code, the user authorizes it in their client (naming the app), and the code matched on
  both sides confirms it. After that, requests flow without re-asking.
- **The user's session services requests** by calling the switchboard's MCP tools —
  `switchboard_take` pulls the next request, `switchboard_deliver` returns the result.
- **Liveness is a fact.** If the daemon dies, an app sees itself go `stale`.

## Use it

Install (Python ≥ 3.11): `uv sync`.

**1. Bring the daemon up at session start** — a `SessionStart` hook (shipped in
`.claude/settings.json`):

```json
{ "hooks": { "SessionStart": [ { "hooks": [
  { "type": "command", "command": "uv run python -m switchboard hook" } ] } ] } }
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

Any language can speak the wire directly — see `switchboard/protocol.py` (newline-delimited
JSON over the loopback port).

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

It is the same broker core and the same five tools as the daemon — only the two faces differ:
the app reaches the core in-process (`ask`), the user's client reaches it over the network.
Still write-ahead, still transports-not-adjudicates.

## The record

Two day-one artifacts: `switchboard/boundary.py` (the nondeterminism boundary, recorded by
flight-recorder) and `ledger/` (the design decisions as checkable data — `uv run --group
ledger python -m ledger.check`). Tests: `uv run pytest`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). © 2026 Xavier Grehant
