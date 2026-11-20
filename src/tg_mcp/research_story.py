import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from .multi_search import PAGE_TIMEOUT, REQUEST_TIMEOUT, MultiSearch, MultiSearchResult
from .page_cursors import CursorPages
from .telegram_search import (
    ReplyPage, RichMessage, StoryAnchor, TelegramSearch, TelegramSearchError,
)


REPLY_BATCH_BUDGET = 4


@dataclass(frozen=True)
class FollowUp:
    queries: tuple[str, ...]
    since: datetime | None
    until: datetime
    limit: int
    max_text_chars: int | None = None


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
class StoryError:
    message: str
    code: str
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class StoryResult:
    chat: str
    message: RichMessage
    replied_to: RichMessage | None
    parent_status: str
    replies: StoryReplies
    follow_up: StoryFollowUp | None
    parent_error: StoryError | None = None
    context_text_included: bool = True


@dataclass(frozen=True)
class _StoryPage:
    chat: str
    chat_id: int
    anchor: StoryAnchor
    replies_limit: int
    offset_id: int | None = None
    use_history: bool = False
    parent_error: StoryError | None = None


@dataclass(frozen=True)
class _ReplyRead:
    hits: list[RichMessage]
    status: str
    offset_id: int | None
    use_history: bool
    error: StoryError | None


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
        parent, replies, author_result = await asyncio.gather(
            self._read_parent(state),
            self._read_replies(state),
            self._read_follow_up(state, follow_up),
        )
        anchor, parent_error = parent
        return self._result(replace(state, anchor=anchor, parent_error=parent_error), replies, author_result)

    async def resume(self, cursor: str) -> StoryResult:
        return await self._pages.get(cursor)

    async def _read_parent(self, state: _StoryPage) -> tuple[StoryAnchor, StoryError | None]:
        anchor = state.anchor
        parent_id = anchor.parent_message_id
        if parent_id is None:
            return anchor, None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), self._requests:
                parent = await self._backend.get_story_parent(state.chat_id, parent_id)
        except TelegramSearchError as exc:
            error = StoryError(str(exc), exc.code, exc.retry_after_seconds)
        except TimeoutError:
            error = StoryError("Telegram parent message request timed out.", "timeout")
        else:
            status = "available" if parent is not None else "unavailable"
            return replace(anchor, replied_to=parent, parent_status=status), None
        return replace(anchor, parent_status="failed"), error

    async def _read_replies(self, state: _StoryPage) -> _ReplyRead:
        anchor = state.anchor.message
        hits: dict[int, RichMessage] = {}
        offset = state.offset_id
        use_history = state.use_history
        status = "more"
        error = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PAGE_TIMEOUT
        for _ in range(REPLY_BATCH_BUDGET):
            if loop.time() >= deadline:
                break
            page_budget = asyncio.timeout_at(deadline)
            try:
                async with page_budget, self._requests, asyncio.timeout(REQUEST_TIMEOUT):
                    page: ReplyPage = await self._backend.get_story_replies(
                        state.chat_id,
                        anchor.message_id,
                        limit=state.replies_limit - len(hits),
                        offset_id=offset,
                        use_history=use_history,
                    )
            except TelegramSearchError as exc:
                error = StoryError(str(exc), exc.code, exc.retry_after_seconds)
                break
            except TimeoutError:
                if not page_budget.expired():
                    error = StoryError("Telegram replies timed out. Retry this reply page.", "timeout")
                break

            hits.update({
                hit.message_id: hit for hit in page.hits
                if hit.message_id > anchor.message_id and hit.date >= anchor.date
                and (offset is None or hit.message_id < offset)
            })
            status = page.status
            if status != "more":
                break
            next_offset = page.next_offset_id
            if next_offset is not None and next_offset <= anchor.message_id:
                status = "complete"
                break
            offset_moved = next_offset is not None and (offset is None or next_offset < offset)
            entered_history = page.use_history and not use_history and next_offset == offset
            if not offset_moved and not entered_history:
                error = StoryError("Telegram returned a reply cursor without progress.", "stalled_offset")
                break
            offset, use_history = next_offset, page.use_history
            if len(hits) >= state.replies_limit:
                break

        if error is not None:
            status = "partial" if hits else "failed"
        return _ReplyRead(
            sorted(hits.values(), key=lambda hit: hit.message_id, reverse=True),
            status, offset, use_history, error,
        )

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
        text_limit = {"max_text_chars": request.max_text_chars} if request.max_text_chars is not None else {}
        result = await self._multi_search.search(
            {state.chat: state.chat_id},
            request.queries,
            since,
            request.until,
            request.limit,
            author=anchor.sender_id,
            after_message_id=anchor.message_id,
            **text_limit,
        )
        status: Literal["complete", "more", "partial"] = "complete"
        if result.partial:
            status = "partial"
        elif result.next_cursor is not None:
            status = "more"
        return StoryFollowUp("same_author_and_query", status, since, request.until, result, None)

    async def _resume_page(self, state: _StoryPage) -> StoryResult:
        return self._result(state, await self._read_replies(state), context_text_included=False)

    def _result(
        self, state: _StoryPage, page: _ReplyRead, follow_up: StoryFollowUp | None = None,
        *, context_text_included: bool = True,
    ) -> StoryResult:
        next_cursor = None
        retry_cursor = None
        error = page.error
        if page.status == "more" or (error is not None and error.code != "stalled_offset"):
            anchor = state.anchor
            if context_text_included:
                parent = anchor.replied_to
                anchor = replace(
                    anchor,
                    message=replace(anchor.message, text=None, caption=None),
                    replied_to=replace(parent, text=None, caption=None) if parent is not None else None,
                )
            continuation = replace(state, anchor=anchor, offset_id=page.offset_id, use_history=page.use_history)
            cursor = self._pages.add(lambda: self._resume_page(continuation))
            if page.status == "more":
                next_cursor = cursor
            else:
                retry_cursor = cursor
        return StoryResult(
            state.chat,
            state.anchor.message,
            state.anchor.replied_to,
            state.anchor.parent_status,
            StoryReplies(
                "direct_replies", page.hits, page.status, next_cursor, retry_cursor,
                error.message if error is not None else None,
                error.code if error is not None else None,
                error.retry_after_seconds if error is not None else None,
            ),
            follow_up,
            state.parent_error,
            context_text_included,
        )
