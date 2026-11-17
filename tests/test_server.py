from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from mcp import Client

from relocation_telegram_mcp.config import ConfigurationError, Settings, load_settings
from relocation_telegram_mcp.server import create_server
from relocation_telegram_mcp.telegram_search import MessageContext, SearchHit, SearchPage, TelegramSearch


class ConfigTests(TestCase):
    def test_only_configured_groups_with_local_session(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(dir=project) as directory:
            config = Path(directory) / "config.toml"
            for session, chats in (
                ("telegram.session", ""),
                ("telegram.session", "relocation = 1234"),
                ("../other.session", "relocation = -1234"),
            ):
                with self.subTest(session=session, chats=chats):
                    config.write_text(
                        f'[telegram]\napi_id = 12345\napi_hash = "example"\nsession = "{session}"\n'
                        f'[chats]\n{chats}\n',
                        encoding="utf-8",
                    )
                    with self.assertRaises(ConfigurationError):
                        load_settings(config)


class ServerTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        project = Path(__file__).resolve().parents[1]
        directory = self.enterContext(TemporaryDirectory(dir=project))
        settings = Settings(
            api_id=12345,
            api_hash="test",
            session_path=Path(directory) / "test.session",
            chats={"relocation": -1001234567890, "housing": -1009876543210},
        )
        self.backend = AsyncMock(spec=TelegramSearch)
        self.server = create_server(settings, self.backend)

    async def test_keyword_search_and_configured_aliases(self) -> None:
        self.backend.search.return_value = SearchPage(hits=[], next_offset_id=None)
        async with Client(self.server, read_timeout_seconds=5) as client:
            self.assertEqual(
                {tool.name for tool in (await client.list_tools()).tools},
                {"list_search_chats", "search_messages", "get_message_context"},
            )
            aliases = await client.call_tool("list_search_chats", {})
            result = await client.call_tool(
                "search_messages", {"chat": "housing", "query": " депозит "}
            )
        self.assertFalse(aliases.is_error)
        self.assertEqual(aliases.structured_content["result"], ["housing", "relocation"])
        self.assertFalse(result.is_error)
        self.backend.search.assert_awaited_once_with(
            -1009876543210, query="депозит", author=None, since=None, until=None,
            limit=20, offset_id=None,
        )
        self.backend.close.assert_awaited_once()

    async def test_author_search_paginates_even_after_empty_page(self) -> None:
        date = datetime(2026, 3, 15, tzinfo=UTC)
        self.backend.search.side_effect = [
            SearchPage(hits=[], next_offset_id=543),
            SearchPage(hits=[SearchHit(501, date, 12345, "Продолжение", None)], next_offset_id=None),
        ]
        request = {
            "chat": "relocation",
            "author": 12345,
            "since": "2026-03-01T00:00:00+03:00",
            "until": "2026-04-01T00:00:00+03:00",
            "limit": 2,
        }
        async with Client(self.server, read_timeout_seconds=5) as client:
            page = await client.call_tool("search_messages", request)
            self.assertFalse(page.is_error)
            self.assertEqual(page.structured_content["hits"], [])
            offset_id = page.structured_content["next_offset_id"]
            self.assertEqual(offset_id, 543)
            page = await client.call_tool("search_messages", {**request, "offset_id": offset_id})
        self.assertFalse(page.is_error)
        self.assertEqual(page.structured_content["hits"][0]["sender_id"], 12345)
        self.assertIsNone(page.structured_content["next_offset_id"])
        self.backend.search.assert_awaited_with(
            -1001234567890, query=None, author=12345,
            since=datetime(2026, 2, 28, 21, tzinfo=UTC),
            until=datetime(2026, 3, 31, 21, tzinfo=UTC),
            limit=2, offset_id=543,
        )

    async def test_rejects_unknown_chat_and_unconstrained_search(self) -> None:
        async with Client(self.server, read_timeout_seconds=5) as client:
            for request in (
                {"chat": "-1001234567890", "query": "депозит"},
                {"chat": "relocation", "query": " "},
                {"chat": "relocation", "author": "display name"},
                {"chat": "relocation", "author": 12345, "limit": 51},
                {"chat": "relocation", "author": 12345, "offset_id": 0},
            ):
                with self.subTest(request=request):
                    result = await client.call_tool("search_messages", request)
                    self.assertTrue(result.is_error)
        self.backend.search.assert_not_awaited()

    async def test_context_tool_checks_chat_and_window_then_returns_reply(self) -> None:
        date = datetime(2026, 3, 15, tzinfo=UTC)
        self.backend.get_context.return_value = MessageContext(
            message=SearchHit(12, date, 12345, "Три недели", None),
            before=[], after=[],
            replied_to=SearchHit(4, date, 23456, "Сколько ждали запись?", None),
        )
        async with Client(self.server, read_timeout_seconds=5) as client:
            for request in (
                {"chat": "unknown", "message_id": 12},
                {"chat": "housing", "message_id": 12, "before": 21},
                {"chat": "housing", "message_id": 0},
            ):
                result = await client.call_tool("get_message_context", request)
                self.assertTrue(result.is_error)
            result = await client.call_tool(
                "get_message_context", {"chat": "housing", "message_id": 12, "before": 0, "after": 0},
            )
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["replied_to"]["message_id"], 4)
        self.backend.get_context.assert_awaited_once_with(-1009876543210, 12, before=0, after=0)
