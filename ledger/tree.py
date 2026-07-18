"""The design ledger — switchboard's own decisions, as data a rule can go red on.

Day one, per the estate's standing practice: every non-obvious decision with the
alternatives it rejected, every load-bearing belief with what would kill it, every known
shortcut with the condition that discharges it. switchboard brokers messages between an
app and a live AI-client session; it is not a quern tree, so quern holds only this record,
not the runtime state. The flight boundary (switchboard/boundary.py) is the other day-one
artifact; the two arrive together with the first commit.

    uv run --group ledger python -m ledger.check
"""

from __future__ import annotations

import os
from pathlib import Path

import quern.grounding  # noqa: F401 — the grounding natives, for any gate rules
from quern import Node, Quern

_ROOT = Path(__file__).resolve().parents[1]


def build() -> Quern:
    from quern.library import consume
    lib, refs = consume(_ROOT, os.environ.get("QUERN_REGISTRY",
                                              _ROOT.parent / "quern-registry"))
    quern = Quern(packages=[next(r for r in refs if r.name == "ledger")])
    quern = lib.effective(quern)
    quern.root.children = [_SINGLETON, _TRANSPORT, _PAIRING, _TRANSPORTS_NOT_ADJUDICATES,
                           _WRITE_AHEAD, _CLIENT_IS_LIVE, _TRANSCRIPT_GAP, _DAEMON_UNRECORDED]
    return quern


_SINGLETON = Node(
    id="one-shared-broker-per-user",
    kind="decision",
    name="The switchboard is a single shared broker per user — spawned idempotently by "
         "a SessionStart hook, guarded by a discovery file, never one-per-app or "
         "one-per-session",
    payload={
        "rationale":
            "The whole value is a channel any app can reach and any of the user's "
            "sessions can service — a shared switchboard, not a private wire. So the "
            "hook that spawns it must be idempotent: the first session to start brings "
            "the daemon up and writes a discovery file (endpoint + pid + nonce); every "
            "later session finds it already live and does nothing. A per-app or "
            "per-session daemon would fragment the channel into wires that cannot see "
            "each other's pairings, which is the opposite of a switchboard.",
        "consequence":
            "One process holds all pairings and the request log; its liveness is a "
            "single fact every app and session reads from the discovery file. The cost "
            "is a machine-wide singleton to supervise (stale discovery file after an "
            "unclean death — hence the liveness nonce below).",
    },
    children=[
        Node(id="alt-per-session-daemon", kind="alternative",
             name="Spawn one switchboard per client session",
             payload={"why":
                      "Then an app paired to session A is invisible to session B, and "
                      "the 'shared channel any app can connect to' becomes N private "
                      "channels. Pairing state could not be reused across the user's "
                      "own sessions — the exact thing the design wants."}),
        Node(id="alt-per-app-daemon", kind="alternative",
             name="Spawn one switchboard per connecting app",
             payload={"why":
                      "No shared broker at all, and the SessionStart hook has nothing "
                      "to spawn (it fires before any app connects). The 'one at a time, "
                      "with authorization' pairing model needs a single arbiter, not "
                      "one per app."}),
    ],
)


_TRANSPORT = Node(
    id="endpoint-is-loopback-tcp",
    kind="decision",
    name="The daemon endpoint is a loopback TCP port plus a discovery file — not a Unix "
         "socket, not a named pipe",
    payload={
        "rationale":
            "Two very different callers must reach the daemon: external apps (any "
            "language) and the MCP server subprocess the client spawns. The endpoint "
            "must be trivially reachable from both, on Windows first. A loopback TCP "
            "port (127.0.0.1) is the one transport that is identical on every OS and "
            "every language's standard library; a discovery file at a known user path "
            "carries the live port, pid and nonce so callers find it without a fixed "
            "port. Unix domain sockets and named pipes each need per-OS code (Windows "
            "UDS is partial and recent; pipe APIs differ), buying locality we do not "
            "need for a localhost broker.",
        "consequence":
            "Bound to 127.0.0.1 only — never a routable interface — so the channel is "
            "local to the machine, and a payload never leaves it via the switchboard. "
            "Access control is the discovery-file nonce plus the pairing handshake, not "
            "filesystem permissions.",
    },
    children=[
        Node(id="alt-unix-socket", kind="alternative",
             name="Use a Unix domain socket at a well-known path",
             payload={"why":
                      "Windows is the primary platform and its UDS support is partial "
                      "and version-gated; a socket file also re-introduces the "
                      "filesystem-permission and stale-inode problems the discovery "
                      "file already solves for TCP."}),
        Node(id="alt-named-pipe", kind="alternative",
             name="Use an OS named pipe",
             payload={"why":
                      "Named-pipe APIs and semantics differ between Windows and POSIX, "
                      "so every app author would need per-OS client code to reach a "
                      "channel whose whole point is being easy to connect to."}),
    ],
)


_PAIRING = Node(
    id="pairing-is-authorized-with-a-matched-code",
    kind="decision",
    name="An app pairs once through an MCP tool the user authorizes, confirmed by a code "
         "shown identically on both sides — never silent, never per-request",
    payload={
        "rationale":
            "Pairing is the trust act: it lets an app put requests in front of the "
            "user's own AI client. So it happens in the client, through an MCP tool the "
            "user must approve, and it names the app. The code shown on both the app and "
            "the switchboard side defeats the confused-deputy case — it proves the app "
            "the user is authorizing is the same app that opened the connection, not "
            "another one racing for the slot. Once paired, requests flow without "
            "re-authorizing each one: per-request prompts would make the channel useless "
            "for anything but a single question.",
        "consequence":
            "One authorization per app, then a durable pairing the app presents on every "
            "request. Compromise of a paired app is the app's problem, by the "
            "transports-not-adjudicates line below; the switchboard's job is to prove "
            "*which* app it is talking to, which the matched code does.",
    },
    children=[
        Node(id="alt-silent-pairing", kind="alternative",
             name="Auto-pair any app that connects, no authorization",
             payload={"why":
                      "Any process on the machine could then push requests into the "
                      "user's client unbidden — the switchboard would be an open relay "
                      "into the session. The user must name and approve the app once."}),
        Node(id="alt-authorize-every-request", kind="alternative",
             name="Prompt the user to authorize each request",
             payload={"why":
                      "Safe but pointless: a channel that needs a human tap per message "
                      "carries nothing an ordinary chat turn would not. The trust "
                      "decision belongs at pairing, made once, revocable."}),
    ],
)


_TRANSPORTS_NOT_ADJUDICATES = Node(
    id="the-channel-transports-it-does-not-adjudicate",
    kind="decision",
    name="The switchboard transports payloads faithfully and records what it carried; it "
         "does not judge, filter, or vouch for what an app sends",
    payload={
        "rationale":
            "Content provenance, trust, and consent over *what* an app sends are the "
            "app's responsibility, not the channel's. A switchboard that adjudicated "
            "payloads would become a policy engine every app must model around, and "
            "would implicitly vouch for content it cannot understand. Its integrity "
            "claim is narrow and keepable: it moved the bytes unchanged and kept a "
            "record of doing so. That record — write-ahead, below — is the estate's "
            "evidence-before-claim doctrine applied to a wire.",
        "consequence":
            "The trust boundary is the pairing (which app) plus the record (what "
            "crossed), never a verdict on the payload. An app that sends something "
            "harmful is accountable for it; the switchboard can show faithfully what it "
            "relayed, which is exactly what a neutral channel should be able to do.",
    },
    children=[
        Node(id="alt-vet-payloads", kind="alternative",
             name="Have the switchboard screen or classify payloads before relaying",
             payload={"why":
                      "Turns a neutral channel into an interested party that vouches "
                      "for content, and forces every app to model the channel's policy. "
                      "Screening is an app-level or client-level concern; the wire's "
                      "only promise is faithful carriage and an honest record."}),
    ],
)


_WRITE_AHEAD = Node(
    id="write-ahead-before-send",
    kind="decision",
    name="A request is recorded (write-ahead) before it is dispatched to the client — "
         "never after the result returns",
    payload={
        "rationale":
            "The estate's founding move is evidence before the claim. A request log "
            "written only on completion loses exactly the requests that matter most — "
            "the ones in flight when the switchboard or the client dies. Writing ahead "
            "means every request that was ever accepted leaves a durable mark before it "
            "can be lost, so a crash is reconstructable from the log rather than "
            "re-derived by guesswork (the flight-recorder discipline, one layer up).",
        "consequence":
            "The log may hold requests that never got a result (in-flight at death) — "
            "which is the point: an unanswered request is a fact worth keeping, not an "
            "omission. Result and status are appended as they resolve.",
    },
    children=[
        Node(id="alt-record-on-completion", kind="alternative",
             name="Record a request only once its result is known",
             payload={"why":
                      "Loses every in-flight request on a crash — the precise failures "
                      "an audit trail exists to explain. A record that forgets what was "
                      "attempted cannot answer 'what happened' when it matters."}),
    ],
)


_CLIENT_IS_LIVE = Node(
    id="the-client-is-a-live-session-not-a-headless-pump",
    kind="hypothesis",
    name="An app request can be serviced by a live, user-attended AI-client session — "
         "the one that spawned the app, or one the user pairs it with — without the user "
         "having to type the request as a turn",
    payload={
        "held_because":
            "The goal is a three-party interaction: the user steers the app from its own "
            "UI and from the client, and the app offloads work to that live client. This "
            "rules out a headless pump the daemon owns — that would be a second, "
            "unattended agent, not the user's session. The mechanism the design bets on "
            "is Claude Code's stream-json turn injection: the switchboard writes the "
            "app's request into the session as a turn the user did not type, serviced "
            "between the user's own turns (idle) or during one (active), with the result "
            "captured on the return path (an MCP tool the client calls) and routed back "
            "to the app.",
        "consequence_if_wrong":
            "If no supported stream-json path lets an app request reach an *attended* "
            "session without a user-initiated turn, v0's live servicing is unachievable "
            "on current Claude Code, and the channel degrades to a recorded request "
            "queue a human drains by hand — useful, but not the switchboard.",
        "note":
            "This is the experimental core and the reason the repo is marked "
            "experimental: it rests on undocumented Claude Code behavior (stream-json "
            "input is documented only for headless -p mode; issue #24594) that may "
            "change without notice. The pairing, liveness, write-ahead, and return-path "
            "plumbing do not depend on it and are built regardless.",
    },
    children=[
        Node(id="no-supported-inject-into-an-attended-session", kind="falsification",
             payload={
                 "claim":
                     "There is no Claude Code facility — documented or stable enough to "
                     "depend on — by which an external process makes an attended session "
                     "take a turn on injected content without the user initiating it. "
                     "Not 'it is undocumented'; not 'mid-turn injects are lossy': that "
                     "the attended-session injection cannot be made to work at all.",
                 "cadence": "at the first end-to-end servicing spike, and at every "
                            "Claude Code release that touches stream-json or hooks",
                 "discharge_route":
                     "Retreat v0 to the recorded-queue shape (pairing + WAL + a "
                     "poll/deliver tool a human triggers), keep the plumbing, and reprice "
                     "'no extra user turn' as a goal awaiting a client facility that "
                     "supports it.",
             }),
    ],
)


_TRANSCRIPT_GAP = Node(
    id="mid-turn-requests-are-not-persisted",
    kind="debt",
    name="A request injected mid-turn influences that turn but is not written to session "
         "history, so it is lost on --resume — a known Claude Code gap the channel "
         "inherits",
    payload={
        "note":
            "Claude Code issue #41230 (closed not-planned): messages fed to a session "
            "during an active turn are processed in memory but never land in the "
            "conversation JSONL, so a --resume replays a history that omits them; "
            "long turns also block the queue and can drop pending messages on disconnect "
            "(#73118). The switchboard cannot fix the client, but it must not pretend "
            "the gap away: it prefers between-turns delivery for anything that must "
            "survive a resume, and its own write-ahead log keeps the request even when "
            "the client's transcript does not.",
    },
    children=[
        Node(id="discharge-when-persistence-or-mirroring-lands", kind="discharge",
             payload={
                 "condition":
                     "Either Claude Code persists mid-turn injected messages (#41230 "
                     "reopened and fixed) and the switchboard delivers mid-turn freely; "
                     "or the switchboard mirrors each serviced request into the session "
                     "history itself (via a between-turns follow-up or a transcript "
                     "write) so no serviced request is absent on --resume — whichever "
                     "lands first, with the mechanism journaled here.",
             }),
    ],
)


_DAEMON_UNRECORDED = Node(
    id="only-the-mcp-surface-is-recorded-in-v0",
    kind="debt",
    name="v0 records the MCP surface (the tool calls) with flight-recorder, but not the "
         "daemon's own socket and client-stream I/O — the channel's most interesting "
         "boundary is declared, not yet taped",
    payload={
        "note":
            "boundary.py names the full nondeterminism boundary — the app socket, the "
            "client stream, the clock, the pairing randomness, process spawning — but "
            "install_mcp only wraps the MCP server process, which is a different process "
            "from the daemon. So in v0 a session's tool calls replay, but the daemon's "
            "accept/relay path does not. This is the spec-studio pattern (record the "
            "surface first, refine inward) applied here, recorded honestly rather than "
            "left implicit.",
    },
    children=[
        Node(id="discharge-record-the-daemon-boundary", kind="discharge",
             payload={
                 "condition":
                     "The daemon process records its own boundary — socket accepts, "
                     "request frames, the client stream read/write, the clock and the "
                     "code randomness — as its own flight chain, so a whole channel "
                     "session (app in, client out) replays end to end, not just the "
                     "tool calls.",
             }),
    ],
)
