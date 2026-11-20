import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, call, patch

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

    async def test_empty_reply_pages_continue_without_repeating_context(self) -> None:
        anchor = replace(self.anchor, text=None, caption="photo caption", message_type="photo", text_status="caption")
        parent = self.message(40)
        self.backend.get_story_anchor.return_value = StoryAnchor(
            message=anchor, replied_to=None, parent_status="pending", is_user_author=True,
            parent_message_id=parent.message_id,
        )
        self.backend.get_story_parent.return_value = parent
        self.backend.get_story_replies.side_effect = [
            *(ReplyPage([], offset, True, "more") for offset in (120, 115, 110, 105, 100, 99, 98, 95)),
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
        self.assertEqual(third.replies.hits[0].text, "message 90")
        self.assertTrue(first.context_text_included)
        self.assertEqual(first.message.caption, "photo caption")
        self.assertEqual(first.replied_to.text, "message 40")
        for page in (second, third):
            self.assertFalse(page.context_text_included)
            self.assertEqual(page.message, replace(anchor, caption=None))
            self.assertEqual(page.replied_to, replace(parent, text=None))
            self.assertIsNone(page.follow_up)
        self.assertIsNotNone(first.follow_up)
        self.backend.get_story_replies.assert_any_await(
            self.chat_id, 50, limit=20, offset_id=105, use_history=True,
        )
        self.backend.get_story_replies.assert_awaited_with(
            self.chat_id, 50, limit=20, offset_id=95, use_history=True,
        )
        self.backend.get_story_anchor.assert_awaited_once()
        self.backend.get_story_parent.assert_awaited_once_with(self.chat_id, 40)
        self.multi.search.assert_awaited_once()

    async def test_follow_up_passes_requested_text_limit(self) -> None:
        until = self.date + timedelta(days=1)
        follow_up = FollowUp(("visa",), None, until, 5, max_text_chars=100)

        await self.research.get_story("community", self.chat_id, 50, follow_up=follow_up)

        self.multi.search.assert_awaited_once_with(
            {"community": self.chat_id}, ("visa",), self.date, until, 5,
            author=12345, after_message_id=50, max_text_chars=100,
        )

    async def test_failed_parent_does_not_discard_original_replies_or_follow_up(self) -> None:
        self.backend.get_story_anchor.return_value = StoryAnchor(
            message=self.anchor, replied_to=None, parent_status="pending", is_user_author=True,
            parent_message_id=40,
        )
        self.backend.get_story_parent.side_effect = TelegramSearchError(
            "Wait 10 seconds.", code="rate_limited", retry_after_seconds=10,
        )
        self.backend.get_story_replies.return_value = ReplyPage(
            hits=[self.message(60)], next_offset_id=None, use_history=False, status="complete",
        )
        follow_up = FollowUp(("visa",), None, self.date + timedelta(days=1), 5)

        result = await self.research.get_story("community", self.chat_id, 50, follow_up=follow_up)

        self.assertIs(result.message, self.anchor)
        self.assertIsNone(result.replied_to)
        self.assertEqual(result.parent_status, "failed")
        self.assertEqual(result.parent_error.code, "rate_limited")
        self.assertEqual(result.parent_error.retry_after_seconds, 10)
        self.assertEqual([hit.message_id for hit in result.replies.hits], [60])
        self.assertEqual(result.follow_up.status, "complete")
        self.backend.get_story_parent.assert_awaited_once_with(self.chat_id, 40)

    async def test_later_reply_timeout_preserves_hits_and_retries_from_last_checkpoint(self) -> None:
        self.backend.get_story_replies.side_effect = [
            ReplyPage([self.message(90)], 90, False, "more"),
            TimeoutError(),
            ReplyPage([self.message(70)], None, False, "complete"),
        ]

        partial = await self.research.get_story("community", self.chat_id, 50, replies_limit=2)
        retried = await self.research.resume(partial.replies.retry_cursor)

        self.assertEqual([hit.message_id for hit in partial.replies.hits], [90])
        self.assertEqual(partial.replies.status, "partial")
        self.assertEqual(partial.replies.error_code, "timeout")
        self.assertIsNone(partial.replies.next_cursor)
        self.assertEqual([hit.message_id for hit in retried.replies.hits], [70])
        self.assertEqual(retried.replies.status, "complete")
        self.assertEqual(self.backend.get_story_replies.await_args_list, [
            call(self.chat_id, 50, limit=2, offset_id=None, use_history=False),
            call(self.chat_id, 50, limit=1, offset_id=90, use_history=False),
            call(self.chat_id, 50, limit=2, offset_id=90, use_history=False),
        ])

    async def test_history_fallback_at_same_offset_is_retained_on_retry(self) -> None:
        self.backend.get_story_replies.side_effect = [
            ReplyPage([], None, True, "more"),
            TimeoutError(),
            ReplyPage([self.message(60)], None, True, "complete"),
        ]

        first = await self.research.get_story("community", self.chat_id, 50)
        retried = await self.research.resume(first.replies.retry_cursor)

        self.assertEqual(first.replies.status, "failed")
        self.assertEqual([hit.message_id for hit in retried.replies.hits], [60])
        self.backend.get_story_replies.assert_awaited_with(
            self.chat_id, 50, limit=20, offset_id=None, use_history=True,
        )

    async def test_reply_page_budget_returns_continuation_from_last_checkpoint(self) -> None:
        async def get_replies(chat_id, message_id, *, limit, offset_id, use_history):
            if offset_id is None:
                return ReplyPage([self.message(90)], 90, False, "more")
            await asyncio.Event().wait()

        self.backend.get_story_replies.side_effect = get_replies
        with patch("tg_mcp.research_story.PAGE_TIMEOUT", 0.05):
            first = await self.research.get_story("community", self.chat_id, 50)
        self.backend.get_story_replies.side_effect = None
        self.backend.get_story_replies.return_value = ReplyPage([self.message(80)], None, False, "complete")
        second = await self.research.resume(first.replies.next_cursor)

        self.assertEqual(first.replies.status, "more")
        self.assertEqual([hit.message_id for hit in first.replies.hits], [90])
        self.assertIsNone(first.replies.retry_cursor)
        self.assertEqual([hit.message_id for hit in second.replies.hits], [80])
        self.backend.get_story_replies.assert_awaited_with(
            self.chat_id, 50, limit=20, offset_id=90, use_history=False,
        )

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
