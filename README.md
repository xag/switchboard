# switchboard

> ## ⚠️ Deprecated — do not build on this
>
> **Status: abandoned, unmaintained, kept only for the record. It may be archived or
> removed.** Nothing here should be used in new work.
>
> The gap switchboard was built to close does not exist as a gap. It is a decision the
> whole ecosystem has made, and switchboard cannot out-engineer it:
>
> - **Nothing initiates a turn but the user.** MCP Apps state the principle outright — "a
>   user is never prompted out of nowhere and every elicitation traces back to something
>   they or their agent started." A queued request is durable and correct and still cannot
>   spend the session's next turn.
> - **The one exception is local-only.** Claude Code can turn a watched process's stdout
>   into an event that starts a turn. No other client can, and it does not exist on web or
>   mobile — which is where an app most wants to reach you.
> - **Sampling is not the thing to replace.** It is deprecated (SEP-2577, protocol
>   2026-07-28) because server→client calls need a persistent connection that MCP v2
>   removes. It also never carried audio, so it was never the route to voice.
> - **For an app the session launches, MCP Apps is simply better.** The app runs inside
>   the conversation, shares the host's state, keeps sampling (where the transport
>   objection does not apply), and needs no daemon, no pairing and no loopback port.
>
> What remains is a narrow case — an app with a life outside the conversation that wants
> the session's intelligence and can wait for the user's next turn. That is a small enough
> claim that it did not justify a channel.
>
> **Use instead:** [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview) for
> anything the session launches; a provider API for anything needing intelligence with no
> human present.
>
> The reasoning, including the alternatives rejected along the way, is in
> [`ledger/`](ledger/) — that is the part worth keeping.

---

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
- **An app pairs once, and stays paired.** The default ceremony: the app shows a code, the
  user authorizes it in their client (naming the app), and the code matched on both sides
  confirms it. An app **the session spawns itself** skips the ceremony — the session mints
  a spawn secret (`switchboard_preauthorize`), hands it over in `SWITCHBOARD_SECRET`, and
  the app redeems it once; launching it was the consent. An **external app** folds the
  ceremony into a share: `pairing_prompt()` returns a paste-able line the user launches in
  their session. A **hosted app** is not asked at all — it owns its own broker, so the
  connector was the consent.

  What the user admitted is remembered in `~/.switchboard/apps.json` (a hash of the token,
  never the token), so a daemon restart does not re-ask a settled question:

  ```
  switchboard apps            # who has been admitted, and how
  switchboard allow <app>     # pre-approve one by name; it never asks
  switchboard forget <app>    # revoke — takes effect on the next request, not the next restart
  ```
- **The user's session services requests** by calling the switchboard's MCP tools —
  `switchboard_take` pulls the next request, `switchboard_deliver` returns the result.
- **Waiting requests nudge the agent** two ways, because one is not enough. The client's
  own lifecycle hooks cover an *active* session: a stop is held (once) while the queue is
  non-empty, a request sent with `urgency="turn"` is surfaced mid-turn, and anything
  waiting rides in with the user's next prompt. But a hook only fires on an event the
  client already generates, so a session parked at the prompt hears nothing — for that,
  **`python -m switchboard listen`** prints one line per queued request and the client
  watches its stdout (in Claude Code, the `Monitor` tool). It announces and never
  consumes: `take` stays the agent's act. The `SessionStart` hook asks the agent to arm
  the listener, since a hook cannot arm it itself — a hook exits, and a process it spawns
  detached writes where nobody reads. The channel still spawns nothing and drives no
  session.
- **Liveness is a fact.** If the daemon dies, an app sees itself go `stale`.

## Use it

Install (Python ≥ 3.11): `uv sync`.

**1. Wire the hooks** — one command, and every session has the channel:

```
switchboard install-hooks              # the user's client settings (~/.claude)
switchboard install-hooks --project .  # or just this project
switchboard install-hooks --dry-run    # look first; undo with uninstall-hooks
```

It merges four entries into the settings file and touches nothing else (a `.bak` is kept):
`SessionStart` brings the daemon up idempotently and asks the agent to arm the listener;
`Stop` / `PostToolUse` / `UserPromptSubmit` surface waiting requests. Each is one cheap
frame to the daemon, and silence if it is down.

Installing the package never writes these by itself. Arranging to run code on someone's
every prompt is a decision to make out loud, so it is a command you run — a channel that
spawns nothing does not get to install itself either.

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

**Guard the surface.** Pairing says *which app* may send work; it does not say who is
calling `switchboard_take`. Without a token, anyone who can reach the URL can read your
requests, deny them to your session, and deliver forged answers back to the app. Pass
`auth_token=` (the user's client sends it as a bearer token), or put your own
authentication in front of the mount. A channel mounted with neither warns on stderr.

```python
from switchboard.embed import Channel, NotPaired

channel = Channel("my-hosted-app", record=my_wal_sink,   # the app's own write-ahead store
                  auth_token=os.environ["SWITCHBOARD_TOKEN"])

# Mount the user-side surface in the app's ASGI server (Starlette / FastAPI):
app.mount("/switchboard", channel.mcp_app())   # its /mcp endpoint is the connector URL

# From an app request handler, offload to the user's live session:
async def handle():
    try:
        return await channel.ask({"question": "..."})
    except NotPaired as np:
        show_the_user(np.code)   # the user matches it in their client to authorize once
```

An app that already serves its own MCP surface can skip the separate mount and put the
tools straight on it with `channel.register_on(mcp)` — then the client that spawned the app
services requests over the same connection, no second connector. This is the shape that
replaces MCP sampling on the app's own surface.

Wake-on-idle works here too, and **without the user installing anything**. Give the channel
its public URL and the connector carries its own arming instruction:

```python
channel = Channel("my-app", record=my_wal_sink, public_url="https://my-app.example/switchboard")
```

The MCP server's `instructions` then tell the agent how to service requests *and* hand it
the exact watcher command to start — a curl loop against `GET /waiting`, a plain-JSON route
mounted beside `/mcp`. Plain HTTP because MCP wants a handshake and a session header before
it will answer, which is no way to arm a shell watcher. `/waiting` reports counts, apps,
request ids and urgencies, and never a payload.

Someone who *does* have switchboard installed can point the local listener at the same
channel instead: `switchboard listen --url https://my-app.example/switchboard/mcp`.

It is the same broker core and the same tools as the daemon — only the faces differ:
the app reaches the core in-process (`ask`), the user's client reaches it over the network.
Still write-ahead, still transports-not-adjudicates.

## The record

Two day-one artifacts: `switchboard/boundary.py` (the nondeterminism boundary, recorded by
flight-recorder) and `ledger/` (the design decisions as checkable data — `uv run --group
ledger python -m ledger.check`). Tests: `uv run pytest`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). © 2026 Xavier Grehant
