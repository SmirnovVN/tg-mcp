import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from .multi_search import REQUEST_TIMEOUT, MultiSearch, MultiSearchResult
from .page_cursors import CursorPages
from .telegram_search import (
    ReplyPage, RichMessage, StoryAnchor, TelegramSearch, TelegramSearchError,
)


@dataclass(frozen=True)
class FollowUp:
    queries: tuple[str, ...]
    since: datetime | None
    until: datetime
    limit: int


@dataclass(frozen=True)
class StoryReplies:
    relationship: Literal["direct_replies"]
    hits: list[RichMessage]
    status: str
    next_cursor: str | None
    retry_cursor: str | None
    error: str | None
    error_code: str | None
    retry_after_seconds: int | None


@dataclass(frozen=True)
class StoryFollowUp:
    relationship: Literal["same_author_and_query"]
    status: Literal["complete", "more", "partial", "unavailable", "empty_interval"]
    since: datetime
    until: datetime
    result: MultiSearchResult | None
    reason: str | None


@dataclass(frozen=True)
class StoryResult:
    chat: str
    message: RichMessage
    replied_to: RichMessage | None
    parent_status: str
    replies: StoryReplies
    follow_up: StoryFollowUp | None


@dataclass(frozen=True)
class _StoryPage:
    chat: str
    chat_id: int
    anchor: StoryAnchor
    replies_limit: int
    offset_id: int | None = None
    use_history: bool = False
    follow_up: StoryFollowUp | None = None


@dataclass(frozen=True)
class _ReplyFailure:
    message: str
    code: str
    retry_after_seconds: int | None = None


class ResearchStory:
    def __init__(
        self,
        backend: TelegramSearch,
        multi_search: MultiSearch,
        requests: asyncio.Semaphore | None = None,
    ) -> None:
        self._backend = backend
        self._multi_search = multi_search
        self._requests = requests if requests is not None else asyncio.Semaphore(3)
        self._pages: CursorPages[StoryResult] = CursorPages()

    async def get_story(
        self,
        chat: str,
        chat_id: int,
        message_id: int,
        *,
        replies_limit: int = 20,
        follow_up: FollowUp | None = None,
    ) -> StoryResult:
        async with asyncio.timeout(REQUEST_TIMEOUT), self._requests:
            anchor = await self._backend.get_story_anchor(chat_id, message_id)
        state = _StoryPage(chat, chat_id, anchor, replies_limit, use_history=anchor.use_history)
        replies, author_result = await asyncio.gather(
            self._read_replies(state),
            self._read_follow_up(state, follow_up),
        )
        return self._result(replace(state, follow_up=author_result), replies)

    async def resume(self, cursor: str) -> StoryResult:
        return await self._pages.get(cursor)

    async def _read_replies(self, state: _StoryPage) -> ReplyPage | _ReplyFailure:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), self._requests:
                return await self._backend.get_story_replies(
                    state.chat_id,
                    state.anchor.message.message_id,
                    limit=state.replies_limit,
                    offset_id=state.offset_id,
                    use_history=state.use_history,
                )
        except TelegramSearchError as exc:
            return _ReplyFailure(str(exc), exc.code, exc.retry_after_seconds)
        except TimeoutError:
            return _ReplyFailure("Telegram replies timed out. Retry this reply page.", "timeout")

    async def _read_follow_up(
        self, state: _StoryPage, request: FollowUp | None,
    ) -> StoryFollowUp | None:
        if request is None:
            return None
        anchor = state.anchor.message
        since = max(anchor.date, request.since) if request.since is not None else anchor.date
        if not state.anchor.is_user_author or anchor.sender_id is None:
            return StoryFollowUp(
                "same_author_and_query", "unavailable", since, request.until, None,
                "The anchor has no identifiable Telegram user as its author.",
            )
        if since >= request.until:
            return StoryFollowUp(
                "same_author_and_query", "empty_interval", since, request.until, None,
                "The requested interval contains no messages after this anchor.",
            )
        result = await self._multi_search.search(
            {state.chat: state.chat_id},
            request.queries,
            since,
            request.until,
            request.limit,
            author=anchor.sender_id,
            after_message_id=anchor.message_id,
        )
        status: Literal["complete", "more", "partial"] = "complete"
        if result.partial:
            status = "partial"
        elif result.next_cursor is not None:
            status = "more"
        return StoryFollowUp("same_author_and_query", status, since, request.until, result, None)

    async def _resume_page(self, state: _StoryPage) -> StoryResult:
        return self._result(state, await self._read_replies(state))

    def _result(self, state: _StoryPage, page: ReplyPage | _ReplyFailure) -> StoryResult:
        next_cursor = None
        retry_cursor = None
        if isinstance(page, _ReplyFailure):
            hits: list[RichMessage] = []
            status = "failed"
            retry_cursor = self._pages.add(lambda: self._resume_page(state))
        else:
            anchor = state.anchor.message
            hits_by_id = {
                hit.message_id: hit for hit in page.hits
                if hit.message_id > anchor.message_id and hit.date >= anchor.date
                and (state.offset_id is None or hit.message_id < state.offset_id)
            }
            hits = sorted(hits_by_id.values(), key=lambda hit: hit.message_id, reverse=True)
            offset = page.next_offset_id
            if offset is not None and offset > anchor.message_id:
                continuation = replace(state, offset_id=offset, use_history=page.use_history)
                next_cursor = self._pages.add(lambda: self._resume_page(continuation))
            status = page.status
        return StoryResult(
            state.chat,
            state.anchor.message,
            state.anchor.replied_to,
            state.anchor.parent_status,
            StoryReplies(
                "direct_replies", hits, status, next_cursor, retry_cursor,
                page.message if isinstance(page, _ReplyFailure) else None,
                page.code if isinstance(page, _ReplyFailure) else None,
                page.retry_after_seconds if isinstance(page, _ReplyFailure) else None,
            ),
            state.follow_up,
        )
