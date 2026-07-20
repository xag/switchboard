"""A messenger whose message waits for the session's idle moment.

The mirror of the button demo's urgency='turn': this sends one message with
urgency='idle', so nothing interrupts the agent mid-turn. When the session next tries
to go idle, the Stop hook holds the stop, the agent takes the message, posts it visibly
to the user, and the acknowledgement comes back here. A session already idle picks it
up at its next nudge (the user's next prompt).

Spawned pre-approved by the switchboard-demos server (demo_idle_message):

    idle_messenger.py <delay_seconds> <message...>   with SWITCHBOARD_SECRET set
"""

from __future__ import annotations

import sys
import time

from switchboard.client import App


def main() -> None:
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    message = " ".join(sys.argv[2:]) or "Hello from the idle messenger."
    if delay:
        time.sleep(delay)
    app = App("idle-messenger")
    ack = app.ask(
        {"kind": "message-for-user", "message": message,
         "instructions": "Post this message visibly to the user in your reply, then "
                         "deliver a short acknowledgement."},
        wait=3600, urgency="idle")
    print(f"acknowledged: {ack}", flush=True)


if __name__ == "__main__":
    main()
