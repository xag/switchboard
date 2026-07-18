"""switchboard — a shared local channel between an app and a live AI-client session.

An app sends a request; the user's own client services it; the result comes back — with no
extra user turn, one app paired at a time, every request recorded before it is sent. The
channel transports faithfully and keeps the record; it does not adjudicate what crosses it.
"""

from __future__ import annotations

__version__ = "0.0.1"
