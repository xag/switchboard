"""The MCP surface the client mounts: the user's side of the switchboard.

Two jobs, both driven by tools the client calls: authorize an app that wants to pair
(`switchboard_pairings` to see who is asking, `switchboard_authorize` to admit them), and
service the requests a paired app sends (`switchboard_take` to pull the next one,
`switchboard_deliver` to return the result). The tools are the return path the ledger
names — an MCP tool is an ordinary subprocess, so it reaches the daemon over the same
loopback wire an app uses.

What this module does NOT decide is *when* the client calls these tools. Servicing a
request "with no extra user turn" is the injection question (the live-session hypothesis)
handled by the hook/spike, not here; here the tools simply exist and work when called,
whether by a human, or by a turn the switchboard injected.

flight-recorder wraps every tool call, so a session's pairings and deliveries land on
tapes under ~/.switchboard/flight — the recorded surface of the channel.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import daemon, discovery, protocol
from .protocol import V


def _endpoint() -> protocol.Endpoint:
    """The live daemon, started if need be. The MCP server is a fine place to ensure the
    channel exists: if the SessionStart hook did not run, the first tool call still finds
    a switchboard."""
    info = discovery.alive() or daemon.ensure_running()
    return discovery.endpoint_of(info)


def register(mcp: FastMCP) -> None:

    @mcp.tool(structured_output=True)
    def switchboard_pairings() -> dict[str, Any]:
        """Apps waiting to pair with this session's channel. Each shows a code; before you
        authorize, confirm the code here matches the one the app is showing the user."""
        return protocol.call(_endpoint(), V.PENDING_PAIRINGS)

    @mcp.tool(structured_output=True)
    def switchboard_authorize(pairing_id: str, code: str) -> dict[str, Any]:
        """Admit an app to the channel. Pass the pairing_id and the code the app is
        showing; a code that does not match the switchboard's is refused, so you cannot
        authorize the wrong app by mistake. After this the app may send requests."""
        return protocol.call(_endpoint(), V.AUTHORIZE, pairing_id=pairing_id, code=code)

    @mcp.tool(structured_output=True)
    def switchboard_deny(pairing_id: str) -> dict[str, Any]:
        """Decline a pairing request."""
        return protocol.call(_endpoint(), V.DENY, pairing_id=pairing_id)

    @mcp.tool(structured_output=True)
    def switchboard_take() -> dict[str, Any]:
        """Pull the next request a paired app has sent, to service it. Returns the app,
        the request_id, and the request payload — or {empty: true} if none is waiting.
        Answer it, then return the result with switchboard_deliver(request_id, result)."""
        return protocol.call(_endpoint(), V.TAKE, wait=0)

    @mcp.tool(structured_output=True)
    def switchboard_deliver(request_id: str, result: Any) -> dict[str, Any]:
        """Return a result for a request you took. This unblocks the waiting app."""
        return protocol.call(_endpoint(), V.DELIVER, request_id=request_id, result=result)


def build_server() -> FastMCP:
    mcp = FastMCP("switchboard")
    register(mcp)
    # Record every tool call from the first — pairings and deliveries — as tapes. A
    # failure to arm recording must be seen, not swallowed, but must not stop the channel.
    try:
        from flight_recorder import install_mcp

        from .boundary import boundary
        install_mcp(boundary(), mcp, directory=str(discovery.HOME / "flight"))
    except Exception as e:  # noqa: BLE001
        print(f"[switchboard] WARNING: flight recording not armed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
    return mcp


def serve() -> int:
    """Serve on stdio — the door the client mounts. stdout belongs to the protocol; the
    human line goes to stderr."""
    discovery.ensure_home()
    print("[switchboard] MCP surface on stdio", file=sys.stderr)
    build_server().run()
    return 0
