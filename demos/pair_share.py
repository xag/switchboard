"""Workflow: share-prompt pairing — for an app pairing from the outside.

Instead of asking the user to eyeball-match a code, the app hands them one paste-able
line (behind a share sheet or a copy button; here, printed). The user launching it in
their session is the acceptance: carrying the code from the app's UI into the client
proves the same possession the eyeball match does, and the code stays single-use and
short-lived.

    uv run python demos/pair_share.py "your question"

Paste the printed line into your client session; the session accepts (or denies) and
then services the request: switchboard_take, answer, switchboard_deliver.
"""

from __future__ import annotations

import sys

from switchboard.client import App


def main() -> None:
    question = " ".join(sys.argv[1:]) or "In one word: what is the capital of Italy?"
    app = App("demo-share")
    print("Paste this into your client session — the paste is the acceptance:\n",
          flush=True)
    print(f"  {app.pairing_prompt()}\n", flush=True)
    app.await_pairing(wait=300)
    print("paired.", flush=True)
    answer = app.ask({"question": question})
    print(f"answer: {answer}", flush=True)


if __name__ == "__main__":
    main()
