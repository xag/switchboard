# demos

One small app per supported workflow. Each sends a question through the channel and
prints the answer your live session delivers. Run them from the repo root; service them
from any Claude Code session with the switchboard MCP surface mounted (`.mcp.json` ships
it here).

| Workflow | Demo | The user's part |
| --- | --- | --- |
| Default ceremony: code matched on both sides | `pair_code.py` | Check the codes match, `switchboard_authorize` |
| Spawn-secret: the session launches the app | `pair_spawned.py` | None — `switchboard_preauthorize`, then spawning it, was the consent |
| Share-prompt: the app hands over a paste-able line | `pair_share.py` | Paste the line into the session — the paste is the acceptance |
| Urgency: mid-turn vs next-idle delivery | `urgency.py` | Authorize once, then watch `take` report each request's urgency |
| Embedded channel: a hosted app, no local daemon | `hosted_notes.py` | Add the printed URL as a remote MCP connector once, then the ceremony as usual |

```
uv run python demos/pair_code.py "In one word: capital of France?"
uv run python demos/pair_spawned.py        # spawned by the session with SWITCHBOARD_SECRET set
uv run python demos/pair_share.py
uv run python demos/urgency.py
uv run --group dev python demos/hosted_notes.py
```

The first four ride the shared local daemon (the SessionStart hook brings it up; any demo
finds it through `~/.switchboard/switchboard.json`). The hosted demo starts no daemon at
all — it embeds the broker and serves its own MCP surface on `127.0.0.1:8737`.

While a demo waits, the hooks do the nudging: a `Stop` is held while its request sits in
the queue, an `urgency="turn"` request is surfaced between tool calls, and anything still
waiting is mentioned when you next prompt.
