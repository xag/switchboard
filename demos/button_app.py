"""A spawned MCP app with two buttons: the same prompt, sent with two urgencies.

Meant to be launched BY the session (the spawn-secret workflow): the session mints a
secret with switchboard_preauthorize(app='button-demo'), spawns this with it in
SWITCHBOARD_SECRET, and the first send pairs silently — no code, no ceremony.

The two buttons differ in exactly one field, which is the whole point:

- **Send mid-turn** (`urgency='turn'`) asks to be surfaced while the agent is working.
  The PostToolUse hook injects it between tool calls, so a busy session picks it up
  without waiting to finish.
- **Send at next idle** (`urgency='idle'`) interrupts nothing. It waits for the agent to
  try to go idle; the Stop hook holds that stop and hands the request over.

Both answers come back into the window, labelled with the urgency that carried them.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk

from switchboard.client import App

PROMPT = "In one sentence: what is this session working on right now?"


def main() -> None:
    app = App("button-demo")
    events: "queue.Queue[tuple[str, str, str]]" = queue.Queue()

    root = tk.Tk()
    root.title("button-demo — one prompt, two urgencies")
    root.geometry("620x330")

    tk.Label(root, text="Prompt to send to the session:", anchor="w").pack(
        fill="x", padx=12, pady=(12, 0))
    entry = tk.Entry(root)
    entry.insert(0, PROMPT)
    entry.pack(fill="x", padx=12, pady=(2, 8))

    row = tk.Frame(root)
    row.pack(pady=4)

    def send(urgency: str) -> None:
        prompt = entry.get().strip()
        if not prompt:
            return
        for b in (turn_btn, idle_btn):
            b.config(state="disabled")
        status.config(
            text=("sent with urgency='turn' — the session should surface it mid-turn…"
                  if urgency == "turn" else
                  "sent with urgency='idle' — it will wait for the session to go idle…"))

        def work() -> None:
            try:
                events.put((urgency, "answered.",
                            str(app.ask({"prompt": prompt}, wait=900, urgency=urgency))))
            except Exception as e:  # noqa: BLE001 — whatever failed, show it in the window
                events.put((urgency, "failed.", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    turn_btn = tk.Button(row, text="Send mid-turn  (urgency='turn')",
                         command=lambda: send("turn"))
    turn_btn.pack(side="left", padx=6)
    idle_btn = tk.Button(row, text="Send at next idle  (urgency='idle')",
                         command=lambda: send("idle"))
    idle_btn.pack(side="left", padx=6)

    # A dead channel is reported in the window, never by exiting: the window IS the app,
    # and a GUI that vanishes on startup looks like a crash. The daemon may come back;
    # the buttons stay live either way.
    status = tk.Label(root, fg="gray", wraplength=580, justify="left",
                      text=("no live switchboard yet — start a session, then send"
                            if app.stale else
                            "not paired yet — the first send pairs silently"))
    status.pack(fill="x", padx=12, pady=(6, 0))

    answer = tk.Message(root, width=580, text="", justify="left")
    answer.pack(fill="both", expand=True, padx=12, pady=8)

    def poll() -> None:
        try:
            urgency, outcome, text = events.get_nowait()
        except queue.Empty:
            pass
        else:
            status.config(text=f"[{urgency}] {outcome}")
            answer.config(text=text)
            for b in (turn_btn, idle_btn):
                b.config(state="normal")
        root.after(100, poll)

    poll()
    root.mainloop()


if __name__ == "__main__":
    main()
