"""Workflow: the embedded channel — a hosted app self-hosting its own switchboard.

No local daemon: this app holds a `Channel` in its own process and serves the user-side
MCP surface over HTTP. The user adds the printed URL to their client as a remote MCP
connector once; the same pairing ceremony then runs in-band (the app shows a code, the
user matches it over the connector), and every ask is serviced by the user's live session
across the network.

    uv run --group dev python demos/hosted_notes.py            # interactive
    uv run --group dev python demos/hosted_notes.py "question"  # one ask, then exit

Add the connector it prints (e.g. `claude mcp add --transport http hosted-notes
http://127.0.0.1:8737/mcp`), then service requests from that session: the code lands in
switchboard_pairings; after authorizing, switchboard_take / switchboard_deliver as usual.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

from switchboard.embed import Channel, NotPaired

PORT = 8737


def main() -> None:
    ch = Channel("hosted-notes")
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def serve() -> None:
        import uvicorn
        asyncio.set_event_loop(loop)
        config = uvicorn.Config(ch.mcp_app(), host="127.0.0.1", port=PORT,
                                log_level="warning", lifespan="on")
        server = uvicorn.Server(config)
        started.set()
        loop.run_until_complete(server.serve())

    threading.Thread(target=serve, daemon=True).start()
    started.wait(5)
    time.sleep(0.5)  # let uvicorn bind before advertising the URL
    print(f"hosted-notes is up. Add the connector to your client once:\n"
          f"  claude mcp add --transport http hosted-notes http://127.0.0.1:{PORT}/mcp\n",
          flush=True)

    def on_loop(coro):
        """Every core touch runs on the server's loop — the channel and its MCP surface
        share one, exactly as a mounted ASGI app and its handlers do."""
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def ask(question: str) -> None:
        while True:
            try:
                answer = on_loop(ch.ask({"question": question}, wait=300))
                print(f"answer: {answer}", flush=True)
                return
            except NotPaired as np:
                print(f"pairing code: {np.code} — in the connector session, "
                      f"switchboard_pairings then switchboard_authorize.", flush=True)
                on_loop(ch.await_paired(wait=300))
                print("paired.", flush=True)

    if len(sys.argv) > 1:
        ask(" ".join(sys.argv[1:]))
        return
    print("type a question (empty line to quit):", flush=True)
    while (q := input("> ").strip()):
        ask(q)


if __name__ == "__main__":
    main()
