"""The embeddable channel's invariants — a hosted app self-hosting its switchboard.

Two levels. The direct tests drive `Channel` and its in-process core on one loop: the app
asks, the user-side verbs service, the write-ahead records — the logic, with no transport.
The HTTP test is the whole claim of issue 2: it stands up the channel's MCP surface on a real
loopback server, connects a real MCP client (the user's client with the connector added), and
proves `ask(request) -> result` across the network with the broker living only in the app's
process. Everything that touches the core runs on the server's loop, since a mounted ASGI app
and its host share one.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from switchboard.embed import Channel, Denied, NotPaired


# -- direct: the core serviced in-process, no transport -------------------------------

def test_first_ask_patches_through_and_is_serviced():
    events: list[dict] = []
    ch = Channel("hosted-notes", record=events.append)

    async def body():
        # No pairing yet: the first ask patches through, carrying a code to show the user.
        with pytest.raises(NotPaired) as ei:
            await ch.ask({"q": 1})
        code = ei.value.code

        # The user authorizes over the (would-be remote) surface — here, the core direct.
        pend = ch.board.pending_pairings()["pairings"][0]
        assert pend["code"] == code
        assert ch.board.authorize({"pairing_id": pend["pairing_id"], "code": code})["ok"]
        assert ch.pairing_status() == "authorized" and ch.paired

        # Now ask, and let the user's side service it concurrently.
        async def service():
            took = await ch.board.take({"wait": 2})
            ch.board.deliver({"request_id": took["request_id"],
                              "result": {"a": took["request"]["q"] + 1}})

        servicing = asyncio.ensure_future(service())
        result = await ch.ask({"q": 41})
        await servicing
        assert result == {"a": 42}

    asyncio.run(body())

    order = [e["event"] for e in events if e.get("request_id") == "r1"]
    assert order == ["request", "result"]  # write-ahead: request strictly before result


def test_denied_pairing_raises():
    ch = Channel("nosy")

    async def body():
        ch.begin_pairing()
        ch.board.deny({"pairing_id": ch._pairing_id})
        with pytest.raises(Denied):
            await ch.await_paired(wait=1, poll=0.05)

    asyncio.run(body())


def test_channel_needs_a_name():
    with pytest.raises(ValueError):
        Channel("   ")


# -- HTTP: the remote MCP surface, broker only in the app's process -------------------

@pytest.fixture
def embedded():
    """A hosted app: the channel's MCP surface on a real loopback server. Yields the
    channel, its URL, the server loop (every core touch is scheduled onto it), and the
    write-ahead sink."""
    import uvicorn

    records: list[dict] = []
    ch = Channel("hosted-notes", record=records.append)
    app = ch.mcp_app()

    loop = asyncio.new_event_loop()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                            lifespan="on")
    server = uvicorn.Server(config)

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "embedded server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"

    yield ch, url, loop, records

    server.should_exit = True
    t.join(timeout=5)


def test_ask_to_result_across_remote_mcp(embedded):
    ch, url, loop, records = embedded

    async def on_server(coro):
        """Run a core-touching coroutine on the server's loop, awaited from ours."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def flow():
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        # The app opens a pairing and shows the code in its own UI.
        code = await on_server(_call(ch.begin_pairing))

        # The user's client — the connector they added — services the channel over HTTP.
        async with streamable_http_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                names = {t.name for t in (await session.list_tools()).tools}
                assert {"switchboard_pairings", "switchboard_authorize",
                        "switchboard_take", "switchboard_deliver"} <= names

                pairings = (await session.call_tool(
                    "switchboard_pairings", {})).structuredContent["pairings"]
                pend = next(p for p in pairings if p["code"] == code)

                auth = (await session.call_tool("switchboard_authorize",
                        {"pairing_id": pend["pairing_id"],
                         "code": pend["code"]})).structuredContent
                assert auth["ok"] and auth["app"] == "hosted-notes"

                assert await on_server(_call(ch.pairing_status)) == "authorized"

                # The app asks; the user's session takes and delivers; the app gets it back.
                ask = asyncio.run_coroutine_threadsafe(ch.ask({"q": 41}), loop)
                took = None
                for _ in range(20):
                    took = (await session.call_tool(
                        "switchboard_take", {})).structuredContent
                    if not took.get("empty"):
                        break
                    await asyncio.sleep(0.05)
                assert took and took["request"] == {"q": 41}

                delivered = (await session.call_tool("switchboard_deliver",
                             {"request_id": took["request_id"],
                              "result": {"a": took["request"]["q"] + 1}})).structuredContent
                assert delivered["ok"]

                assert await asyncio.wrap_future(ask) == {"a": 42}

    asyncio.run(asyncio.wait_for(flow(), timeout=20))

    # Write-ahead held across the remote round trip: the request was on disk before its
    # result, even though a networked client serviced it.
    order = [e["event"] for e in records if e.get("request_id") == "r1"]
    assert order == ["request", "result"]


async def _call(fn):
    """Adapt a sync core call into a coroutine so it runs on the server loop."""
    return fn()


def test_remote_authorize_refuses_a_mismatched_code(embedded):
    ch, url, loop, _ = embedded

    async def on_server(coro):
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def flow():
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        await on_server(_call(ch.begin_pairing))
        async with streamable_http_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                pend = (await session.call_tool(
                    "switchboard_pairings", {})).structuredContent["pairings"][0]
                bad = (await session.call_tool("switchboard_authorize",
                       {"pairing_id": pend["pairing_id"],
                        "code": "000000"})).structuredContent
                assert bad["ok"] is False and "mismatch" in bad["error"]

    asyncio.run(asyncio.wait_for(flow(), timeout=20))
