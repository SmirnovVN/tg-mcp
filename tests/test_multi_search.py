import asyncio
from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from mcp import Client

from tg_mcp.server import create_server
from tg_mcp.telegram_search import ReplyPage, RichMessage, RichSearchPage, StoryAnchor, TelegramSearch, TelegramSearchError


class MultiSearchTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.backend = AsyncMock(spec=TelegramSearch)
        self.server = create_server({"a": -1, "b": -2}, self.backend)

    def message(
        self, message_id: int, day: int, text: str | None = None, caption: str | None = None,
    ) -> RichMessage:
        return RichMessage(
            message_id=message_id,
            date=datetime(2026, 11, day, tzinfo=UTC),
            sender_id=123,
            text=text,
            caption=caption,
            message_type="text" if text is not None else "photo",
            text_status="text" if text is not None else "caption" if caption is not None else "absent",
            url=None,
        )

    async def call(self, client: Client, arguments: dict, tool: str = "search_messages_multi") -> dict:
        result = await client.call_tool(tool, arguments)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def test_overlap_at_page_boundary_and_cursor_replay(self) -> None:
        shared = self.message(80, 12, caption="Visa and term")
        streams = {
            (-1, "visa"): [self.message(90, 13, "Visa"), shared],
            (-1, "term"): [shared, self.message(60, 10, "term")],
            (-2, "visa"): [self.message(80, 12)],
            (-2, "term"): [],
        }

        async def search(chat_id, *, query, offset_id, limit, **_filters):
            remaining = [hit for hit in streams[chat_id, query] if offset_id is None or hit.message_id < offset_id]
            page = remaining[:limit]
            return RichSearchPage(page, page[-1].message_id if len(remaining) > limit else None)

        self.backend.search_rich.side_effect = search
        async with Client(self.server, read_timeout_seconds=5) as first, Client(self.server, read_timeout_seconds=5) as other:
            page = await self.call(first, {"chats": ["a", "b", "a"], "queries": ["visa", "term", " visa "], "limit": 1})
            self.assertEqual(self.backend.search_rich.await_count, 4)
            sequence = list(page["hits"])
            while page["next_cursor"]:
                cursor = {"cursor": page["next_cursor"]}
                page, replay = await asyncio.gather(self.call(first, cursor), self.call(other, cursor))
                self.assertEqual(page, replay)
                sequence.extend(page["hits"])

        self.assertEqual([(hit["chat"], hit["message_id"]) for hit in sequence], [("a", 90), ("b", 80), ("a", 80), ("a", 60)])
        self.assertEqual(self.backend.search_rich.await_count, 6)
        self.assertEqual(sequence[1]["match_sources"], {"visa": "unknown"})
        self.assertEqual(sequence[1]["text_status"], "absent")
        self.assertEqual(sequence[2]["matched_queries"], ["visa", "term"])
        self.assertEqual(sequence[2]["match_sources"], {"visa": "caption", "term": "caption"})
        self.assertEqual(sequence[2]["caption"], "Visa and term")

    async def test_partial_failure_retries_at_failed_stream_offset(self) -> None:
        available = False

        async def search(chat_id, *, query, offset_id, **_filters):
            if chat_id == -2:
                if offset_id is None:
                    return RichSearchPage([self.message(200, 19, "visa")], 200)
                if not available:
                    raise TelegramSearchError("Wait.", code="rate_limited", retry_after_seconds=15)
                self.assertEqual(offset_id, 200)
                return RichSearchPage([self.message(190, 18, "visa")], None)
            return RichSearchPage([self.message(50, 15, "visa"), self.message(40, 14, "visa")], None)

        self.backend.search_rich.side_effect = search
        async with Client(self.server, read_timeout_seconds=5) as client:
            result = await self.call(client, {"chats": ["a", "b"], "queries": ["visa"], "limit": 2})
            self.assertEqual([hit["message_id"] for hit in result["hits"]], [200, 50])
            self.assertTrue(result["partial"])
            self.assertEqual(result["errors"][0]["chat"], "b")
            self.assertEqual(result["errors"][0]["retry_after_seconds"], 15)
            rest = await self.call(client, {"cursor": result["next_cursor"]})
            self.assertTrue(rest["partial"])
            self.assertEqual([hit["message_id"] for hit in rest["hits"]], [40])
            self.assertIsNone(rest["next_cursor"])
            available = True
            retry = await self.call(client, {"cursor": result["retry_cursor"]})
            self.assertFalse(retry["partial"])
            self.assertEqual([hit["message_id"] for hit in retry["hits"]], [190])

    async def test_empty_continuable_stream_does_not_lose_buffered_messages(self) -> None:
        async def search(chat_id, *, offset_id, **_filters):
            if chat_id == -1:
                return RichSearchPage([self.message(100, 10, "visa")], None)
            if offset_id is None or offset_id > 180:
                return RichSearchPage([], (offset_id or 200) - 1)
            return RichSearchPage([self.message(150, 16, "visa")], None)

        self.backend.search_rich.side_effect = search
        async with Client(self.server, read_timeout_seconds=5) as client:
            result = await self.call(client, {"chats": ["a", "b"], "queries": ["visa"]})
            self.assertEqual(result["hits"], [])
            self.assertFalse(result["partial"])
            rest = await self.call(client, {"cursor": result["next_cursor"]})
            self.assertEqual([hit["message_id"] for hit in rest["hits"]], [150, 100])
            self.assertIsNone(rest["next_cursor"])

    async def test_unknown_chat_and_cursor_filter_are_rejected(self) -> None:
        async with Client(self.server, read_timeout_seconds=5) as client:
            for arguments in (
                {"chats": ["a", "unknown"], "queries": ["visa"]},
                {"chats": ["a"], "queries": [" "]},
                {"chats": ["a"], "queries": ["visa"], "since": "2026-11-10T00:00:00Z", "until": "2026-11-09T00:00:00Z"},
                {"cursor": "x" * 32, "limit": 1},
            ):
                self.assertTrue((await client.call_tool("search_messages_multi", arguments)).is_error)
            result = await client.call_tool("search_messages_multi", {"cursor": "x" * 32})
            self.assertTrue(result.is_error)
            self.assertIn("cursor_expired", result.content[0].text)
        self.backend.search_rich.assert_not_awaited()

    async def test_story_tool_outputs_direct_replies_and_thematic_follow_ups(self) -> None:
        anchor = self.message(50, 10, "filed")
        self.backend.get_story_anchor.return_value = StoryAnchor(anchor, None, "none", True)
        self.backend.get_story_replies.return_value = ReplyPage([self.message(52, 10, "reply")], None, False, "complete")
        self.backend.search_rich.return_value = RichSearchPage([self.message(60, 12, "visa")], None)
        async with Client(self.server, read_timeout_seconds=5) as client:
            story = await self.call(client, {
                "chat": "a", "message_id": 50,
                "follow_up": {"queries": ["visa"], "until": "2026-11-15T00:00:00+03:00"},
            }, "get_message_story")
        self.assertEqual(story["replies"]["relationship"], "direct_replies")
        self.assertEqual(story["replies"]["hits"][0]["message_id"], 52)
        self.assertEqual(story["follow_up"]["relationship"], "same_author_and_query")
        self.assertEqual(story["follow_up"]["result"]["hits"][0]["message_id"], 60)
        self.backend.search_rich.assert_awaited_once()
        self.assertEqual(self.backend.search_rich.await_args.kwargs["after_message_id"], 50)
        self.assertEqual(self.backend.search_rich.await_args.kwargs["author"], 123)
