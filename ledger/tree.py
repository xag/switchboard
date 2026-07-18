"""The design ledger — switchboard's own decisions, as data a rule can go red on.

    uv run --group ledger python -m ledger.check
"""

from __future__ import annotations

import os
from pathlib import Path

import quern.grounding  # noqa: F401 — grounding natives, for any gate rules
from quern import Node, Quern

_ROOT = Path(__file__).resolve().parents[1]


def build() -> Quern:
    from quern.library import consume
    lib, refs = consume(_ROOT, os.environ.get("QUERN_REGISTRY",
                                              _ROOT.parent / "quern-registry"))
    quern = Quern(packages=[next(r for r in refs if r.name == "ledger")])
    quern = lib.effective(quern)
    quern.root.children = [_SINGLETON, _TRANSPORT, _PAIRING, _NEUTRAL, _WRITE_AHEAD,
                           _EXISTING_SESSION]
    return quern


_SINGLETON = Node(
    id="one-shared-broker-per-user",
    kind="decision",
    name="One shared broker per user, spawned idempotently by a SessionStart hook",
    payload={
        "rationale":
            "The value is a channel any app can reach and any of the user's sessions can "
            "service. So the hook that starts it is idempotent: the first session brings "
            "the daemon up and writes a discovery file; later ones find it and do nothing.",
    },
    children=[
        Node(id="alt-per-session", kind="alternative",
             name="One broker per session",
             payload={"why": "Then a pairing in one session is invisible to another — "
                             "N private wires, not a shared channel."}),
    ],
)


_TRANSPORT = Node(
    id="endpoint-is-loopback-tcp",
    kind="decision",
    name="The endpoint is a loopback TCP port plus a discovery file",
    payload={
        "rationale":
            "Apps in any language and the MCP server must both reach the daemon on "
            "Windows first. Loopback TCP is identical on every OS and standard library; "
            "a discovery file carries the ephemeral port, pid and nonce. Bound to "
            "127.0.0.1 only, so nothing leaves the machine via the channel.",
    },
    children=[
        Node(id="alt-uds-or-pipe", kind="alternative",
             name="Unix socket / named pipe",
             payload={"why": "Per-OS client code for a localhost channel whose point is "
                             "being easy to connect to; Windows UDS is partial."}),
    ],
)


_PAIRING = Node(
    id="pairing-is-authorized-with-a-matched-code",
    kind="decision",
    name="An app pairs once, authorized by the user, confirmed by a code matched on both "
         "sides — never silent, never per-request",
    payload={
        "rationale":
            "Pairing lets an app put requests in front of the user's client, so the user "
            "approves it once and names it; the code matched on the app and the "
            "switchboard proves it is that app, not another racing for the slot. After "
            "that, requests flow without re-authorizing each one.",
    },
    children=[
        Node(id="alt-silent", kind="alternative",
             name="Auto-pair any app that connects",
             payload={"why": "Any process could push requests into the user's client "
                             "unbidden — an open relay into the session."}),
    ],
)


_NEUTRAL = Node(
    id="the-channel-transports-it-does-not-adjudicate",
    kind="decision",
    name="The switchboard transports payloads faithfully and records what it carried; it "
         "does not judge what an app sends",
    payload={
        "rationale":
            "Provenance and consent over what an app sends are the app's concern. A "
            "channel that screened payloads would vouch for content it cannot understand "
            "and force every app around its policy. Its promise is narrow and keepable: "
            "it moved the bytes unchanged and kept the record.",
    },
    children=[
        Node(id="alt-screen", kind="alternative",
             name="Screen or classify payloads before relaying",
             payload={"why": "Turns a neutral wire into an interested party; screening "
                             "is an app or client concern."}),
    ],
)


_WRITE_AHEAD = Node(
    id="write-ahead-before-send",
    kind="decision",
    name="A request is recorded before it is dispatched, never after the result returns",
    payload={
        "rationale":
            "Evidence before the claim. A log written only on completion loses the "
            "requests in flight when something dies — the ones that matter most. Writing "
            "ahead means every accepted request leaves a durable mark before it can be "
            "lost.",
    },
    children=[
        Node(id="alt-on-completion", kind="alternative",
             name="Record only once the result is known",
             payload={"why": "Loses every in-flight request on a crash — the failures an "
                             "audit trail exists to explain."}),
    ],
)


_EXISTING_SESSION = Node(
    id="services-in-the-users-existing-session",
    kind="decision",
    name="Requests are serviced in the user's existing client session, reached only "
         "through MCP and hooks — switchboard is client-agnostic and spawns nothing",
    payload={
        "rationale":
            "The point is a three-party interaction: the user steers the app from its UI "
            "and from the client, and the app offloads to the session the user is already "
            "in. So switchboard is an MCP surface (pairing plus the take/deliver return "
            "path) and a hook, nothing more. It launches no client, drives no session, "
            "and knows nothing of any specific one — a request reaching the app is "
            "serviced by whatever session the user has open.",
    },
    children=[
        Node(id="alt-spawn-a-client", kind="alternative",
             name="Spawn and drive a client the daemon owns",
             payload={"why": "A second agent bound to one vendor, not the user's session "
                             "— it breaks the three-party interaction and ties the "
                             "channel to a single client."}),
    ],
)
