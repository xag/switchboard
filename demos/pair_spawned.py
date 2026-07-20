"""Workflow: spawn-secret pre-approval — for an app the session launches itself.

No code, no ceremony, no user action: the session that spawns this app mints a single-use
secret and passes it in SWITCHBOARD_SECRET; launching the app was the consent. The client
library finds the secret in the environment and redeems it silently on the first ask.

From your client session:

    1. switchboard_preauthorize(app='demo-spawned')     -> returns the secret
    2. spawn this with the secret in its environment:
       SWITCHBOARD_SECRET=<secret> uv run python demos/pair_spawned.py "your question"

Then service the request as usual: switchboard_take, answer, switchboard_deliver.
"""

from __future__ import annotations

import os
import sys

from switchboard.client import SECRET_ENV, App


def main() -> None:
    if not os.environ.get(SECRET_ENV):
        sys.exit(f"{SECRET_ENV} is not set — this demo is meant to be spawned by your "
                 f"session.\nIn the client: switchboard_preauthorize(app='demo-spawned'),"
                 f"\nthen spawn:      {SECRET_ENV}=<secret> uv run python "
                 f"demos/pair_spawned.py")
    question = " ".join(sys.argv[1:]) or "In one word: what is the capital of Japan?"
    app = App("demo-spawned")
    print("spawn secret found — pairing silently, no code shown to anyone", flush=True)
    answer = app.ask({"question": question})
    print(f"answer: {answer}", flush=True)


if __name__ == "__main__":
    main()
