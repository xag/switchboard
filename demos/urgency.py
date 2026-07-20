"""Workflow: urgency — how a request asks to be surfaced in the session.

A request rides with an urgency: 'idle' (the default) waits for the agent's next idle
moment — the Stop hook holds the stop while the queue is non-empty — while 'turn' asks to
be surfaced mid-turn, which the PostToolUse hook injects as context between tool calls.
This demo pairs once (the default ceremony), then sends one of each concurrently and
prints the answers as they land.

    uv run python demos/urgency.py

Watch the session side: `queue_status` counts the 'turn' request under `interject`, and
`switchboard_take` returns each request's urgency alongside its payload.
"""

from __future__ import annotations

import threading

from switchboard.client import App


def main() -> None:
    app = App("demo-urgency")
    answer = {}

    def ask(tag: str, question: str, urgency: str) -> None:
        answer[tag] = app.ask({"question": question}, wait=300, urgency=urgency)
        print(f"{tag} answer: {answer[tag]}", flush=True)

    code = app.begin_pairing()
    print(f"pairing code: {code} — authorize in your client session "
          f"(switchboard_pairings, then switchboard_authorize).", flush=True)
    app.await_pairing(wait=300)

    idle = threading.Thread(target=ask, args=(
        "idle", "Whenever you next go idle: in one word, what is 2+2?", "idle"))
    idle.start()
    ask("turn", "Mid-turn if you can: in one word, what color is the sky?", "turn")
    idle.join()


if __name__ == "__main__":
    main()
