from contextlib import chdir
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from mcp import Client
from telethon import types

from tg_mcp.config import ConfigurationError, HttpSettings, Settings, load_settings
from tg_mcp.server import _serve, create_server, main
from tg_mcp.telegram_search import (
    MessageContext, SearchHit, SearchPage, TelegramSearch,
)
from tg_mcp.telegram_session import _hold_session_lock


class ConfigTests(TestCase):
    def test_launch_uses_http_with_default_or_explicit_config(self) -> None:
        project = Path(__file__).resolve().parents[1]
        token = "a" * 48
        http = HttpSettings(port=8765, token=token)
        with TemporaryDirectory(dir=project) as directory:
            config = Path(directory) / "config.toml"
            config.with_name("telegram.session").touch()
            config.write_text(
                '[telegram]\napi_id = 12345\napi_hash = "test"\nsession = "telegram.session"\n'
                '[chats]\ncommunity = -1234\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, r"\[http\]\.token"):
                load_settings(config)
            with config.open("a", encoding="utf-8") as stream:
                stream.write(f'[http]\ntoken = "{token}"\n')

            other_directory = config.parent / "other"
            other_directory.mkdir()
            for cwd, args in (
                (config.parent, []),
                (other_directory, ["--config", "../config.toml", "--transport", "streamable-http"]),
            ):
                with self.subTest(args=args):
                    client = AsyncMock()
                    client.get_me.return_value = types.User(id=12345)

                    async def serve_while_locked(_server, settings: HttpSettings) -> None:
                        self.assertEqual(settings, http)
                        with self.assertRaisesRegex(ConfigurationError, "already in use"):
                            with _hold_session_lock(config.with_name("telegram.session")):
                                pass

                    with (
                        chdir(cwd),
                        patch("sys.argv", ["tg-mcp", *args]),
                        patch("tg_mcp.telegram_session.TelegramClient", return_value=client),
                        patch("tg_mcp.server.serve_http", side_effect=serve_while_locked) as serve,
                    ):
                        main()

                    serve.assert_awaited_once()
                    client.connect.assert_awaited_once()
                    client.disconnect.assert_awaited_once()

    def test_only_configured_groups_with_local_session(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(dir=project) as directory:
            config = Path(directory) / "config.toml"
            for session, chats in (
                ("telegram.session", ""),
                ("telegram.session", "community = 1234"),
                ("../other.session", "community = -1234"),
            ):
                with self.subTest(session=session, chats=chats):
                    config.write_text(
                        f'[telegram]\napi_id = 12345\napi_hash = "example"\nsession = "{session}"\n'
                        f'[chats]\n{chats}\n'
                        f'[http]\ntoken = "{"a" * 48}"\n',
                        encoding="utf-8",
                    )
                    with self.assertRaises(ConfigurationError):
                        load_settings(config)


class ServerTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        project = Path(__file__).resolve().parents[1]
        directory = self.enterContext(TemporaryDirectory(dir=project))
        self.settings = Settings(
            api_id=12345,
            api_hash="test",
            session_path=Path(directory) / "test.session",
            chats={"community": -1001234567890, "housing": -1009876543210},
            http=HttpSettings(port=8765, token="a" * 48),
        )
        self.backend = AsyncMock(spec=TelegramSearch)
        self.server = create_server(self.settings.chats, self.backend)

    async def test_keyword_search_and_configured_aliases(self) -> None:
        self.backend.search.return_value = SearchPage(hits=[], next_offset_id=None)
        async with Client(self.server, read_timeout_seconds=5) as client:
            self.assertEqual(
                {tool.name for tool in (await client.list_tools()).tools},
                {
                    "list_search_chats", "search_messages", "get_message_context",
                    "search_messages_multi", "get_message_story",
                },
            )
            aliases = await client.call_tool("list_search_chats", {})
            result = await client.call_tool(
                "search_messages", {"chat": "housing", "query": " €500 deposit "}
            )
        self.assertFalse(aliases.is_error)
        self.assertEqual(aliases.structured_content["result"], ["community", "housing"])
        self.assertFalse(result.is_error)
        self.backend.search.assert_awaited_once_with(
            -1009876543210, query="€500 deposit", author=None, since=None, until=None,
            limit=20, offset_id=None,
        )

    async def test_startup_failure_releases_the_session_before_serving_http(self) -> None:
        self.settings.session_path.touch()
        client = AsyncMock()
        client.get_me.return_value = None
        with (
            patch("tg_mcp.telegram_session.TelegramClient", return_value=client),
            patch("tg_mcp.server.serve_http") as serve,
            self.assertRaisesRegex(ConfigurationError, "A Telegram user session is required"),
        ):
            await _serve(self.settings)

        serve.assert_not_awaited()
        client.disconnect.assert_awaited_once()
        with _hold_session_lock(self.settings.session_path):
            pass

    async def test_author_search_paginates_even_after_empty_page(self) -> None:
        date = datetime(2026, 3, 15, tzinfo=UTC)
        self.backend.search.side_effect = [
            SearchPage(hits=[], next_offset_id=543),
            SearchPage(hits=[SearchHit(501, date, 12345, "Follow-up", None)], next_offset_id=None),
        ]
        request = {
            "chat": "community",
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
                {"chat": "-1001234567890", "query": "€500 deposit"},
                {"chat": "community", "query": " "},
                {"chat": "community", "author": "display name"},
                {"chat": "community", "author": 12345, "limit": 51},
                {"chat": "community", "author": 12345, "offset_id": 0},
                {"chat": "community", "author": True},
                {"chat": "community", "author": 12345, "limit": True},
                {"chat": "community", "author": 12345, "since": 1772323200},
                {"chat": "community", "author": 12345, "since": "2026-03-01T12:00:00"},
            ):
                with self.subTest(request=request):
                    result = await client.call_tool("search_messages", request)
                    self.assertTrue(result.is_error)
        self.backend.search.assert_not_awaited()

    async def test_context_tool_checks_chat_and_window_then_returns_reply(self) -> None:
        date = datetime(2026, 3, 15, tzinfo=UTC)
        self.backend.get_context.return_value = MessageContext(
            message=SearchHit(12, date, 12345, "Three weeks", None),
            before=[], after=[],
            replied_to=SearchHit(4, date, 23456, "How long did you wait for an appointment?", None),
        )
        async with Client(self.server, read_timeout_seconds=5) as client:
            for request in (
                {"chat": "unknown", "message_id": 12},
                {"chat": "housing", "message_id": 12, "before": 21},
                {"chat": "housing", "message_id": 0},
                {"chat": "housing", "message_id": True},
            ):
                result = await client.call_tool("get_message_context", request)
                self.assertTrue(result.is_error)
            result = await client.call_tool(
                "get_message_context", {"chat": "housing", "message_id": 12, "before": 0, "after": 0},
            )
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["replied_to"]["message_id"], 4)
        self.backend.get_context.assert_awaited_once_with(-1009876543210, 12, before=0, after=0)
