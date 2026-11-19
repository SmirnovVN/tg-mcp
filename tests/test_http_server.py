import asyncio
from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from httpx2 import ASGITransport, AsyncClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from tg_mcp.config import HttpSettings
from tg_mcp.http_server import create_http_app
from tg_mcp.server import create_server
from tg_mcp.telegram_search import RichMessage, RichSearchPage, SearchHit, SearchPage, TelegramSearch


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

    async def test_multi_search_cursor_continues_in_another_http_client(self) -> None:
        date = datetime(2026, 11, 1, tzinfo=UTC)
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
