from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from tg_mcp.multi_search import MultiSearch, MultiSearchResult
from tg_mcp.research_story import FollowUp, ResearchStory
from tg_mcp.telegram_search import (
    ReplyPage, RichMessage, StoryAnchor, TelegramSearch, TelegramSearchError,
)


class ResearchStoryTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.date = datetime(2026, 11, 1, tzinfo=UTC)
        self.chat_id = -1001234567890
        self.backend = AsyncMock(spec=TelegramSearch)
        self.multi = AsyncMock(spec=MultiSearch)
        self.research = ResearchStory(self.backend, self.multi)
        self.anchor = self.message(50)
        self.backend.get_story_anchor.return_value = StoryAnchor(
            message=self.anchor, replied_to=None, parent_status="none", is_user_author=True,
        )
        self.backend.get_story_replies.return_value = ReplyPage(
            hits=[], next_offset_id=None, use_history=False, status="complete",
        )
        self.multi.search.return_value = MultiSearchResult(
            hits=[], errors=[], partial=False, next_cursor=None, retry_cursor=None,
        )

    def message(self, message_id: int, date: datetime | None = None) -> RichMessage:
        return RichMessage(
            message_id=message_id,
            date=date or self.date,
            sender_id=12345,
            text=f"message {message_id}",
            caption=None,
            message_type="text",
            text_status="text",
            url=f"https://t.me/c/1234567890/{message_id}",
        )

    async def test_follow_up_keeps_same_timestamp_and_strict_anchor_id(self) -> None:
        until = self.date + timedelta(days=1)
        request = FollowUp(("deposit", "visa"), self.date - timedelta(days=3), until, 5)

        result = await self.research.get_story(
            "community", self.chat_id, 50, replies_limit=3, follow_up=request,
        )

        self.assertIs(result.message, self.anchor)
        self.assertEqual(result.replies.relationship, "direct_replies")
        self.assertEqual(result.follow_up.relationship, "same_author_and_query")
        self.assertEqual(result.follow_up.since, self.date)
        self.assertEqual(result.follow_up.status, "complete")
        self.multi.search.assert_awaited_once_with(
            {"community": self.chat_id}, ("deposit", "visa"), self.date, until, 5,
            author=12345, after_message_id=50,
        )

    async def test_empty_reply_pages_continue_and_preserve_follow_up_result(self) -> None:
        self.backend.get_story_replies.side_effect = [
            ReplyPage(hits=[], next_offset_id=100, use_history=True, status="more"),
            ReplyPage(hits=[], next_offset_id=95, use_history=True, status="more"),
            ReplyPage(
                hits=[self.message(90), self.message(90), self.message(50), self.message(40)],
                next_offset_id=None, use_history=True, status="complete",
            ),
        ]
        follow_up = FollowUp(("visa",), None, self.date + timedelta(days=1), 5)

        first = await self.research.get_story("community", self.chat_id, 50, follow_up=follow_up)
        second = await self.research.resume(first.replies.next_cursor)
        third = await self.research.resume(second.replies.next_cursor)

        for page in (first, second):
            self.assertEqual(page.replies.status, "more")
            self.assertEqual(page.replies.hits, [])
            self.assertIsNone(page.replies.retry_cursor)
        self.assertEqual([hit.message_id for hit in third.replies.hits], [90])
        self.assertEqual(third.replies.status, "complete")
        self.assertIs(third.message, first.message)
        self.assertIs(third.follow_up, first.follow_up)
        self.backend.get_story_replies.assert_any_await(
            self.chat_id, 50, limit=20, offset_id=100, use_history=True,
        )
        self.backend.get_story_replies.assert_awaited_with(
            self.chat_id, 50, limit=20, offset_id=95, use_history=True,
        )
        self.backend.get_story_anchor.assert_awaited_once()
        self.multi.search.assert_awaited_once()

    async def test_reply_failure_retains_anchor_and_retries_only_replies(self) -> None:
        self.backend.get_story_replies.side_effect = [
            TelegramSearchError("Wait 10 seconds.", code="rate_limited", retry_after_seconds=10),
            ReplyPage(hits=[self.message(60)], next_offset_id=None, use_history=False, status="complete"),
        ]

        failed = await self.research.get_story("community", self.chat_id, 50)
        retried = await self.research.resume(failed.replies.retry_cursor)

        self.assertIs(failed.message, self.anchor)
        self.assertEqual(failed.replies.status, "failed")
        self.assertEqual(failed.replies.error_code, "rate_limited")
        self.assertEqual(failed.replies.retry_after_seconds, 10)
        self.assertEqual([hit.message_id for hit in retried.replies.hits], [60])
        self.backend.get_story_anchor.assert_awaited_once()
        self.multi.search.assert_not_awaited()

    async def test_follow_up_empty_interval_is_explicit(self) -> None:
        result = await self.research.get_story(
            "community", self.chat_id, 50,
            follow_up=FollowUp(("visa",), None, self.date, 5),
        )

        self.assertEqual(result.follow_up.status, "empty_interval")
        self.multi.search.assert_not_awaited()

    async def test_backend_history_requirement_and_unsupported_status_are_preserved(self) -> None:
        self.backend.get_story_anchor.return_value = StoryAnchor(
            message=self.anchor, replied_to=None, parent_status="none", is_user_author=True,
            use_history=True,
        )
        self.backend.get_story_replies.return_value = ReplyPage(
            hits=[], next_offset_id=None, use_history=True, status="unsupported",
        )

        result = await self.research.get_story("community", self.chat_id, 50)

        self.assertEqual(result.replies.status, "unsupported")
        self.backend.get_story_replies.assert_awaited_once_with(
            self.chat_id, 50, limit=20, offset_id=None, use_history=True,
        )

    async def test_channel_author_keeps_replies_without_user_follow_up(self) -> None:
        self.backend.get_story_anchor.return_value = StoryAnchor(
            message=self.anchor, replied_to=None, parent_status="none", is_user_author=False,
        )

        result = await self.research.get_story(
            "community", self.chat_id, 50,
            follow_up=FollowUp(("visa",), None, self.date + timedelta(days=1), 5),
        )

        self.assertEqual(result.follow_up.status, "unavailable")
        self.backend.get_story_replies.assert_awaited_once()
        self.multi.search.assert_not_awaited()
