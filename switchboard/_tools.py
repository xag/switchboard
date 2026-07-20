"""The user-side MCP tools, defined once for every deployment.

The client mounts the same five tools whether the broker is a loopback daemon on the user's
machine or a hosted app's own server across the network: authorize an app that wants to pair
(`switchboard_pairings`, `switchboard_authorize`, `switchboard_deny`) and service what a
paired app sends (`switchboard_take`, `switchboard_deliver`). Defining them here keeps the
two transports from drifting — the verbs and their descriptions are identical by
construction.

`register(mcp, handlers)` binds the tools to a `Handlers` object. The local surface's
handlers dial the daemon over the loopback wire; the embeddable surface's call the in-process
core directly. Each handler returns a dict or an awaitable of one, so a blocking wire call and
an async core call both fit.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Protocol, Union

from mcp.server.fastmcp import FastMCP

Reply = Union[dict, Awaitable[dict]]


class Handlers(Protocol):
    def pairings(self) -> Reply: ...
    def authorize(self, pairing_id: str, code: str) -> Reply: ...
    def deny(self, pairing_id: str) -> Reply: ...
    def preauthorize(self, app: str) -> Reply: ...
    def take(self) -> Reply: ...
    def deliver(self, request_id: str, result: Any) -> Reply: ...


async def _resolve(reply: Reply) -> dict:
    return await reply if inspect.isawaitable(reply) else reply


def register(mcp: FastMCP, handlers: Handlers) -> None:

    @mcp.tool(structured_output=True)
    async def switchboard_pairings() -> dict[str, Any]:
        """Apps waiting to pair with this session's channel. Each shows a code; before you
        authorize, confirm the code here matches the one the app is showing the user."""
        return await _resolve(handlers.pairings())

    @mcp.tool(structured_output=True)
    async def switchboard_authorize(pairing_id: str, code: str) -> dict[str, Any]:
        """Admit an app to the channel. Pass the pairing_id and the code the app is
        showing; a code that does not match the switchboard's is refused, so you cannot
        authorize the wrong app by mistake. After this the app may send requests."""
        return await _resolve(handlers.authorize(pairing_id, code))

    @mcp.tool(structured_output=True)
    async def switchboard_deny(pairing_id: str) -> dict[str, Any]:
        """Decline a pairing request."""
        return await _resolve(handlers.deny(pairing_id))

    @mcp.tool(structured_output=True)
    async def switchboard_preauthorize(app: str) -> dict[str, Any]:
        """Mint a spawn secret for an app THIS SESSION is about to launch, so it pairs
        with no code ceremony — launching the app is the authorization. Pass the returned
        secret to the app when spawning it (the SWITCHBOARD_SECRET environment variable is
        the convention); the app redeems it once and then sends requests as a paired app.
        Only preauthorize an app you are spawning yourself, never one that asked you to."""
        return await _resolve(handlers.preauthorize(app))

    @mcp.tool(structured_output=True)
    async def switchboard_take() -> dict[str, Any]:
        """Pull the next request a paired app has sent, to service it. Returns the app,
        the request_id, and the request payload — or {empty: true} if none is waiting.
        Answer it, then return the result with switchboard_deliver(request_id, result)."""
        return await _resolve(handlers.take())

    @mcp.tool(structured_output=True)
    async def switchboard_deliver(request_id: str, result: Any) -> dict[str, Any]:
        """Return a result for a request you took. This unblocks the waiting app."""
        return await _resolve(handlers.deliver(request_id, result))
