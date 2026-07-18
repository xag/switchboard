"""switchboard — a channel between an app and a live AI-client session.

An app sends a request; the user's own client services it; the result comes back — one app
paired at a time, every request recorded before it is sent. The channel transports faithfully
and keeps the record; it does not adjudicate what crosses it.

One broker core (`core.Switchboard`), two deployments: a loopback-TCP `daemon` shared by
same-machine apps, and an `embed.Channel` a hosted app self-hosts to expose the same surface
over remote MCP. The core is transport-free; only the two faces differ.
"""

from __future__ import annotations

__version__ = "0.0.1"
