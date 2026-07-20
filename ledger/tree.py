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
                           _EXISTING_SESSION, _CORE_IS_TRANSPORT_FREE, _EMBED_SELF_HOSTS,
                           _SPAWN_SECRET, _HOOKS_NUDGE, _SHARE_RIDES_AUTHORIZE,
                           _LISTENER, _TOLERATE_OLDER_DAEMON, _CHANNEL_DEATH_IS_TOLD]
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


_CORE_IS_TRANSPORT_FREE = Node(
    id="broker-core-is-transport-free",
    kind="decision",
    meta={"amended": "d57d3703dc10 the rationale said 'the five user-side MCP tools' and "
                     "there are now seven (preauthorize, then waiting). Only the count "
                     "moved: the claim — defined once, bound to either transport's "
                     "handlers — is exactly what it was, and adding a tool to both faces "
                     "at once is that claim being kept, not broken."},
    name="The broker core is one transport-free state machine; each deployment is a thin "
         "shell around the same instance",
    payload={
        "rationale":
            "Pairing, the take/deliver return path, write-ahead and liveness are the value; "
            "the wire that carries them is not. Keeping them in `core.Switchboard` — "
            "dict-in / dict-out verbs, no socket — lets the loopback daemon and the "
            "embeddable library reuse the identical verbs and logic, so a second deployment "
            "is a shell, not a fork that drifts. The user-side MCP tools are likewise "
            "defined once and bound to either transport's handlers.",
    },
    children=[
        Node(id="alt-reimplement-per-transport", kind="alternative",
             name="Reimplement the broker for each transport",
             payload={"why": "Two copies of pairing and the return path drift apart; a fix "
                             "or a guard lands in one and not the other. The issue asked to "
                             "factor the core out precisely to avoid that."}),
    ],
)


_SPAWN_SECRET = Node(
    id="spawning-an-app-is-its-authorization",
    kind="decision",
    name="An app the session spawns itself pairs by redeeming a spawn secret the session "
         "minted — the code ceremony is skipped because the consent already happened",
    payload={
        "rationale":
            "The code matched on both sides proves to the user that the app asking is the "
            "app shown. When the session itself launches the app, the same party sits on "
            "both sides of that proof: choosing to spawn was the authorization. So "
            "`switchboard_preauthorize` mints a single-use secret with the same TTL as a "
            "pending code, the spawner hands it over (SWITCHBOARD_SECRET), and the app "
            "redeems it once with `pair_claim` — recorded in the WAL like any authorize. "
            "This does not weaken the never-silent rule: an app that was not handed a "
            "secret still faces the full ceremony.",
    },
    children=[
        Node(id="alt-ceremony-anyway", kind="alternative",
             name="Run the code match even for apps the session spawns",
             payload={"why": "Re-asks consent already given; a ceremony that is always "
                             "rubber-stamped trains the user to click through the ones "
                             "that matter."}),
        Node(id="alt-hand-a-live-token", kind="alternative",
             name="Pass a live token at spawn instead of a claimable secret",
             payload={"why": "Token minting would leave the broker, and the redemption "
                             "would leave no mark. A claim keeps minting inside, records "
                             "the event, and bounds a leaked environment by single use "
                             "plus TTL."}),
    ],
)


_HOOKS_NUDGE = Node(
    id="hooks-nudge-the-agent-they-do-not-drive-it",
    kind="decision",
    name="Waiting requests reach the agent through the client's own lifecycle hooks — a "
         "held stop, a mid-turn note for urgency='turn', a line on the user's prompt — "
         "never by switchboard driving a session",
    payload={
        "rationale":
            "The daemon already listens in the background; what was missing was the nudge "
            "toward the agent. Hooks are the one client-agnostic place to stand: Stop is "
            "blocked (once — stop_hook_active passes the second) while requests wait, so "
            "'idle' delivery means 'at the first idle moment'; PostToolUse injects only "
            "what an app marked urgency='turn'; UserPromptSubmit mentions the rest. Each "
            "is one cheap queue_status frame, and every failure degrades to silence — a "
            "down channel never costs the user a turn.",
    },
    children=[
        Node(id="alt-daemon-drives-the-client", kind="alternative",
             name="Have the daemon wake or drive a client process itself",
             payload={"why": "Binds the channel to one vendor's client and breaks "
                             "services-in-the-users-existing-session — the daemon would "
                             "become a second agent."}),
        Node(id="alt-block-until-drained", kind="alternative",
             name="Block every stop until the queue is empty",
             payload={"why": "A request the agent cannot service would trap the session "
                             "in an endless turn; blocking once surfaces the queue "
                             "without taking the user hostage."}),
    ],
)


_LISTENER = Node(
    id="a-listener-reaches-the-idle-session",
    kind="decision",
    name="A listener process announces each queued request on stdout, so a request reaches "
         "a session parked at the prompt — it announces, it never consumes",
    payload={
        "supersedes":
            "hooks-nudge-the-agent-they-do-not-drive-it, whose rationale claims hooks are "
            "'the one client-agnostic place to stand'. That claim is false and this entry "
            "corrects it: a hook fires only on an event the client already generates, so a "
            "session parked at the prompt hears nothing until the user speaks. The hooks "
            "decision stands for the active session; it never covered the idle one. The "
            "error survived review and was caught only when a user clicked a demo button "
            "four times and nothing happened.",
        "rationale":
            "Issue 4 asked for a background process that 'wakes up the agent if idle', and "
            "the hooks cannot: they fire only inside a turn. `switchboard listen` runs "
            "outside the turn loop and prints one line per new request; the client watches "
            "that stdout, and a line arriving while the session is idle reaches the agent "
            "on its own. It only announces — `take` stays the agent's act over MCP, so a "
            "request is still written ahead, taken once, and delivered by the session. "
            "switchboard still spawns nothing and drives no client: it writes a line and "
            "the client decides what that is worth.",
        "note":
            "The arming is the client's, not a hook's: a SessionStart hook can start a "
            "process, but nothing would read its stdout, and an unread listener is no "
            "better than the silence it replaced. In Claude Code the agent arms it with "
            "the Monitor tool. A client with no way to watch a stream keeps the hooks and "
            "loses only the idle case.",
    },
    children=[
        Node(id="alt-listener-takes-the-request", kind="alternative",
             name="Let the listener take requests and hand them over pre-serviced",
             payload={"why": "Then the queue is drained by something that cannot answer, "
                             "and take-once moves out of the session's hands. Announcing "
                             "keeps every guarantee the return path already had."}),
        Node(id="alt-hook-spawns-the-listener", kind="alternative",
             name="Have the SessionStart hook spawn the listener detached",
             payload={"why": "A detached process's stdout reaches nobody, so it would "
                             "wake no one — the appearance of the feature without the "
                             "fact of it."}),
    ],
)


_TOLERATE_OLDER_DAEMON = Node(
    id="a-client-tolerates-an-older-daemon",
    kind="decision",
    name="A newer client meeting an older daemon degrades to what that daemon can say, and "
         "says so — it never goes silently mute",
    payload={
        "rationale":
            "One shared daemon per user outlives the sessions it serves, so a client "
            "carrying today's code routinely meets a daemon started days ago. The listener "
            "reads `waiting` when the daemon offers it and falls back to the counts every "
            "vintage reports when it does not. Being vaguer is a cost; being silent is a "
            "defect, because silence is indistinguishable from 'nothing is waiting'.",
        "note":
            "Written after the failure it names: the first listener read a queue_status "
            "field the running daemon predated, so it polled a shape that was never there "
            "and printed nothing for three clicks. The same mixed-vintage fault appeared "
            "three times in one session — a stale MCP surface, a stale daemon, a stale "
            "field — which is what makes it a design stance and not a patch.",
    },
    children=[
        Node(id="alt-require-a-matching-daemon", kind="alternative",
             name="Refuse to run against a daemon older than the client",
             payload={"why": "Turns every upgrade into a forced restart that drops live "
                             "pairings and queued requests, to avoid a degradation the "
                             "client can simply describe."}),
    ],
)


_SHARE_RIDES_AUTHORIZE = Node(
    id="share-pairing-rides-authorize",
    kind="decision",
    name="An external app pairs by handing the user a paste-able prompt that carries the "
         "pairing_id and code — the same authorize verb, no second consent path",
    payload={
        "rationale":
            "`pairing_prompt` folds the ceremony into one act: the user carrying the "
            "prompt from the app's share sheet into their session proves the same "
            "possession the eyeball match does, and launching it is the acceptance. The "
            "code lands in the transcript, which is acceptable exactly because a code is "
            "already single-use, short-lived, and bound to one pairing — properties the "
            "matched-code decision established.",
    },
    children=[
        Node(id="alt-dedicated-link-secret", kind="alternative",
             name="Mint a distinct high-entropy link secret with its own verb",
             payload={"why": "A second consent path drifts from authorize and doubles "
                             "what the user must trust; the existing code already has "
                             "the bounds that matter."}),
    ],
)


_CHANNEL_DEATH_IS_TOLD = Node(
    id="a-replaced-channel-is-reported-not-papered-over",
    kind="decision",
    name="The user-side surface remembers the daemon's nonce: if the channel it was using "
         "died and a new one took its place, the caller is told, never handed a fresh "
         "empty queue",
    payload={
        "rationale":
            "The MCP surface starts a daemon when none answers, which is right for a first "
            "call and wrong for a replacement: pairings and the queue live in memory and "
            "die with the process. Reporting `channel: 'replaced'` keeps liveness a fact at "
            "the surface, the way the client library already clears an app's token on a "
            "nonce change. A down daemon is likewise an error, not an empty result.",
        "note":
            "The failure that earned it: a daemon died holding a queued request, `take` "
            "quietly started a replacement, and answered `empty` one line after the Stop "
            "hook had said a request was waiting. An empty queue reads as 'nothing was "
            "waiting', which is the one thing it was not.",
    },
    children=[
        Node(id="alt-silently-restart", kind="alternative",
             name="Start a replacement daemon and answer from it as if nothing happened",
             payload={"why": "Makes the surface lie in the exact case the record exists "
                             "to explain — a lost request looks like no request."}),
    ],
)


_EMBED_SELF_HOSTS = Node(
    id="a-hosted-app-self-hosts-its-channel",
    kind="decision",
    name="A hosted app embeds the broker and exposes the surface over remote MCP itself — "
         "no central relay we run",
    payload={
        "rationale":
            "The loopback daemon only reaches same-machine apps. A hosted app instead holds "
            "a `Channel` in its own process: it reaches the core in-process (`ask`), and "
            "serves the user-side tools over streamable-HTTP for the user's client to add as "
            "a connector. app->client is direct, consent is the user adding the connector "
            "plus the unchanged pairing handshake, and there is nothing for us to operate or "
            "be trusted with.",
    },
    children=[
        Node(id="alt-hosted-relay", kind="alternative",
             name="A relay we host in the middle",
             payload={"why": "Puts us in the path of every request as an operator and a "
                             "trusted party; embedding keeps the channel the app's own. "
                             "Revisit only for an app that genuinely cannot self-host."}),
    ],
)
