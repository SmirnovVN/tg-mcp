import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from httpx2 import ASGITransport, AsyncClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from telethon import TelegramClient, errors, functions, types, utils

from tg_mcp.config import HttpSettings
from tg_mcp.http_server import create_http_app
from tg_mcp.server import create_server
from tg_mcp.telegram_search import RichMessage, RichSearchPage, SearchHit, SearchPage, TelegramSearch
from tg_mcp.telegram_session import SessionClient


class HttpServerTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = HttpSettings(port=8739, token="a" * 48)
        self.url = f"http://127.0.0.1:{self.http.port}/mcp"
        self.authorization = {"Authorization": f"Bearer {self.http.token}"}
        self.chat_id = -1001234567890
        self.backend = AsyncMock(spec=TelegramSearch)
        self.app = create_http_app(create_server({"community": self.chat_id}, self.backend), self.http)

    async def search(self, query: str, mode: str) -> CallToolResult:
        async with AsyncClient(
            transport=ASGITransport(app=self.app), headers=self.authorization,
        ) as http:
            transport = streamable_http_client(self.url, http_client=http)
            async with Client(transport, mode=mode, read_timeout_seconds=5) as client:
                return await client.call_tool(
                    "search_messages", {"chat": "community", "query": query},
                )

    async def test_two_http_clients_search_concurrently(self) -> None:
        searches_started: set[str] = set()
        both_searching = asyncio.Event()

        async def search_backend(chat_id: int, *, query: str, **_filters) -> SearchPage:
            self.assertEqual(chat_id, self.chat_id)
            searches_started.add(query)
            if len(searches_started) == 2:
                both_searching.set()
            await asyncio.wait_for(both_searching.wait(), timeout=4)
            hit = SearchHit(42, datetime(2026, 11, 17, tzinfo=UTC), 12345, query, None)
            return SearchPage(hits=[hit], next_offset_id=None)

        self.backend.search.side_effect = search_backend
        async with self.app.router.lifespan_context(self.app):
            async with asyncio.timeout(8):
                results = await asyncio.gather(
                    self.search("housing", mode="legacy"),
                    self.search("visas", mode="auto"),
                )

        self.assertEqual(self.backend.search.await_count, 2)
        for result, query in zip(results, ("housing", "visas"), strict=True):
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(result.structured_content["hits"][0]["text"], query)

    async def test_six_http_clients_share_four_slots(self) -> None:
        release = asyncio.Event()
        four_started = asyncio.Event()
        active = 0
        peak = 0

        async def search_backend(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                four_started.set()
            try:
                await release.wait()
                return SearchPage([], None)
            finally:
                active -= 1

        self.backend.search.side_effect = search_backend
        async with self.app.router.lifespan_context(self.app), asyncio.timeout(8):
            async with asyncio.TaskGroup() as group:
                calls = [group.create_task(self.search(str(i), "auto")) for i in range(6)]
                await four_started.wait()
                # Give the other clients a chance to enter before releasing any slot.
                await asyncio.sleep(0.05)
                release.set()
        self.assertEqual(peak, 4)
        self.assertTrue(all(not task.result().is_error for task in calls))

    async def test_flood_wait_is_shared_across_clients_and_traced_without_content(self) -> None:
        session = SessionClient(None, 12345, "test")
        peer = types.InputPeerChannel(1234567890, 1)
        session.get_input_entity = AsyncMock(return_value=peer)
        server = create_server({"community": utils.get_peer_id(peer)}, TelegramSearch(session))
        clock = 100.0
        anchor = types.Message(
            id=10, peer_id=types.PeerChannel(peer.channel_id), date=datetime.now(UTC),
            message="private anchor text", from_id=types.PeerUser(12345),
        )

        async def dispatch(request, *args):
            if isinstance(request, functions.messages.GetHistoryRequest) and clock == 100.0:
                raise errors.FloodWaitError(request=request, capture=5)
            return SimpleNamespace(messages=[] if isinstance(request, functions.messages.SearchRequest) else [anchor])

        with (
            patch.object(TelegramClient, "__call__", new_callable=AsyncMock, side_effect=dispatch) as rpc,
            patch("tg_mcp.telegram_session.monotonic", new=lambda: clock),
            self.assertLogs("tg_mcp.diagnostics", level="INFO") as logs,
        ):
            async with Client(server, mode="legacy") as first, Client(server) as other:
                failed = await first.call_tool("get_message_story", {"chat": "community", "message_id": 10})
                retry = failed.structured_content["retry_cursor"]
                self.assertEqual(failed.structured_content["retry_after_seconds"], 5)
                clock += 3
                blocked = await other.call_tool("get_message_story", {"cursor": retry})
                self.assertEqual(blocked.structured_content["retry_after_seconds"], 2)
                self.assertEqual(rpc.await_count, 1)
                ok = await other.call_tool("search_messages", {"chat": "community", "query": "private query"})
                self.assertFalse(ok.is_error)
                clock += 2
                recovered = await other.call_tool("get_message_story", {"cursor": retry})
                self.assertEqual(recovered.structured_content["message"]["text"], anchor.message)
                self.assertEqual(rpc.await_count, 4)
        output = "\n".join(logs.output)
        for field in ("method=GetHistoryRequest", "status=cooldown", "retry_after_seconds=2",
                      "rpc_count=0", "duration_ms=", "cursor_created=", "cursor_used="):
            self.assertIn(field, output)
        for private in (anchor.message, "private query", retry):
            self.assertNotIn(private, output)

    async def test_wrapped_flood_wait_blocks_only_the_original_method(self) -> None:
        session = SessionClient(None, 12345, "test", receive_updates=False, flood_sleep_threshold=0)
        peer = types.InputPeerChannel(1234567890, 1)
        session.get_input_entity = AsyncMock(return_value=peer)
        history = functions.messages.GetHistoryRequest(
            peer=peer, offset_id=0, offset_date=None, add_offset=0, limit=1, max_id=0, min_id=0, hash=0,
        )
        clock = 100.0

        def send(request, **kwargs):
            self.assertIsInstance(request, functions.InvokeWithoutUpdatesRequest)
            future = asyncio.get_running_loop().create_future()
            if isinstance(request.query, functions.messages.GetHistoryRequest) and clock == 100.0:
                future.set_exception(errors.FloodWaitError(request=request, capture=5))
            else:
                future.set_result(SimpleNamespace(messages=[]))
            return future

        with (
            patch.object(session._sender, "send", side_effect=send) as rpc,
            patch("tg_mcp.telegram_session.monotonic", new=lambda: clock),
        ):
            with self.assertRaises(errors.FloodWaitError):
                await session(history)
            clock += 3
            with self.assertRaises(errors.FloodWaitError) as blocked:
                await session(history)
            self.assertEqual(blocked.exception.seconds, 2)
            self.assertEqual(rpc.call_count, 1)
            await session(functions.updates.GetStateRequest())
            clock += 2
            await session(history)
            self.assertEqual(rpc.call_count, 3)

    async def test_multi_search_cursor_continues_in_another_http_client(self) -> None:
        date = datetime(2026, 11, 1, tzinfo=UTC)
        self.backend.get_search_start.return_value = 0
        self.backend.search_rich.side_effect = [
            RichSearchPage([RichMessage(42, date, 12345, "visa", None, "text", "text", None)], 42),
            RichSearchPage([RichMessage(41, date, 12345, "visa", None, "text", "text", None)], None),
        ]

        async def search(mode: str, arguments: dict) -> dict:
            async with AsyncClient(
                transport=ASGITransport(app=self.app), headers=self.authorization,
            ) as http:
                transport = streamable_http_client(self.url, http_client=http)
                async with Client(transport, mode=mode, read_timeout_seconds=5) as client:
                    result = await client.call_tool("search_messages_multi", arguments)
                    self.assertFalse(result.is_error, result.content)
                    return result.structured_content

        async with self.app.router.lifespan_context(self.app):
            first = await search("legacy", {"chats": ["community"], "queries": ["visa"], "limit": 1})
            second = await search("auto", {"cursor": first["next_cursor"]})

        self.assertEqual(first["hits"][0]["message_id"], 42)
        self.assertEqual(second["hits"][0]["message_id"], 41)
        self.assertIsNone(second["next_cursor"])

    async def test_rejects_invalid_authentication_host_and_origin(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_messages",
                "arguments": {"chat": "community", "query": "housing"},
            },
        }
        rejected = (
            ({}, 401),
            ({"Authorization": "Bearer wrong-token"}, 401),
            ({**self.authorization, "Host": "other.example"}, 421),
            ({**self.authorization, "Origin": "https://other.example"}, 403),
        )
        async with (
            self.app.router.lifespan_context(self.app),
            AsyncClient(
                transport=ASGITransport(app=self.app),
                headers={"Accept": "application/json, text/event-stream"},
            ) as http,
        ):
            for headers, status in rejected:
                with self.subTest(status=status, headers=headers.keys()):
                    response = await http.post(self.url, headers=headers, json=request)
                    self.assertEqual(response.status_code, status)
        self.backend.search.assert_not_awaited()
