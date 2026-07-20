"""Workflow: the default pairing ceremony.

The app opens a pairing and shows a six-digit code in its own UI (here, the console). The
user, in their client session, lists waiting pairings (`switchboard_pairings`), checks the
code shown there matches this one, and authorizes. The match proves the app asking is the
app shown; after it, requests flow without re-asking.

    uv run python demos/pair_code.py "In one word: capital of France?"

Service it from any session of yours: switchboard_take, answer, switchboard_deliver.
"""

from __future__ import annotations

import sys

from switchboard.client import App


def main() -> None:
    question = " ".join(sys.argv[1:]) or "In one word: what is the capital of France?"
    app = App("demo-pair-code")
    answer = app.pair_and_ask(
        {"question": question},
        show_code=lambda c: print(
            f"pairing code: {c}\n"
            f"In your client session: switchboard_pairings, check the code matches, "
            f"then switchboard_authorize.", flush=True))
    print(f"answer: {answer}", flush=True)


if __name__ == "__main__":
    main()
