"""The MCP surface the local client mounts: the user's side of the loopback daemon.

The five user-side tools are defined once in `_tools`; here they are bound to handlers that
reach the shared daemon over the same loopback wire an app uses (an MCP tool is an ordinary
subprocess, so it dials 127.0.0.1 like anything else). The embeddable deployment binds the
identical tools to the in-process core instead — see `embed.py`. When the session calls them
is the client's concern, not switchboard's.

flight-recorder wraps every tool call, so a session's pairings and deliveries land on tapes
under ~/.switchboard/flight.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import _tools, daemon, discovery, protocol
from .protocol import V


def _endpoint() -> protocol.Endpoint:
    """The live daemon, started if need be. The MCP server is a fine place to ensure the
    channel exists: if the SessionStart hook did not run, the first tool call still finds
    a switchboard."""
    info = discovery.alive() or daemon.ensure_running()
    return discovery.endpoint_of(info)


class _WireHandlers:
    """The user-side verbs, each a single frame to the daemon over loopback TCP."""

    def pairings(self) -> dict[str, Any]:
        return protocol.call(_endpoint(), V.PENDING_PAIRINGS)

    def authorize(self, pairing_id: str, code: str) -> dict[str, Any]:
        return protocol.call(_endpoint(), V.AUTHORIZE, pairing_id=pairing_id, code=code)

    def deny(self, pairing_id: str) -> dict[str, Any]:
        return protocol.call(_endpoint(), V.DENY, pairing_id=pairing_id)

    def take(self) -> dict[str, Any]:
        return protocol.call(_endpoint(), V.TAKE, wait=0)

    def deliver(self, request_id: str, result: Any) -> dict[str, Any]:
        return protocol.call(_endpoint(), V.DELIVER, request_id=request_id, result=result)


def register(mcp: FastMCP) -> None:
    _tools.register(mcp, _WireHandlers())


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
