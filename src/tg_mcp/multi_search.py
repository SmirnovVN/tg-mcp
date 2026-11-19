import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from .page_cursors import CursorPages
from .telegram_search import RichMessage, TelegramSearch, TelegramSearchError

REQUEST_TIMEOUT = 8
PAGE_TIMEOUT = 25
MAX_STREAMS = 32


@dataclass(frozen=True)
class MultiSearchHit(RichMessage):
    chat: str
    matched_queries: list[str]
    match_sources: dict[str, str]


@dataclass(frozen=True)
class SearchIssue:
    chat: str
    query: str
    code: str
    message: str
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class MultiSearchResult:
    hits: list[MultiSearchHit]
    errors: list[SearchIssue]
    partial: bool
    next_cursor: str | None
    retry_cursor: str | None


@dataclass(frozen=True)
class _Filters:
    since: datetime | None
    until: datetime
    limit: int
    author: int | None
    after_message_id: int | None


@dataclass(frozen=True)
class _Stream:
    chat: str
    chat_id: int
    query: str
    offset: int | None = None
    hits: tuple[RichMessage, ...] = ()
    exhausted: bool = False
    oldest: tuple[datetime, int] | None = None


def _message_order(hit: RichMessage) -> tuple[datetime, int]:
    return hit.date, hit.message_id


def _match_source(hit: RichMessage, query: str) -> str:
    if query.casefold() in (hit.caption or "").casefold():
        return "caption"
    if query.casefold() in (hit.text or "").casefold():
        return "text"
    return "unknown"


class MultiSearch:
    def __init__(self, backend: TelegramSearch, requests: asyncio.Semaphore) -> None:
        self._backend = backend
        self._requests = requests
        self._pages: CursorPages[MultiSearchResult] = CursorPages()

    async def search(
        self,
        chats: dict[str, int],
        queries: tuple[str, ...],
        since: datetime | None,
        until: datetime,
        limit: int,
        *,
        author: int | None = None,
        after_message_id: int | None = None,
    ) -> MultiSearchResult:
        streams = tuple(_Stream(chat, chat_id, query) for chat, chat_id in chats.items() for query in queries)
        filters = _Filters(since, until, limit, author, after_message_id)
        return await self._page(filters, streams)

    async def resume(self, cursor: str) -> MultiSearchResult:
        return await self._pages.get(cursor)

    async def _fetch(
        self, stream: _Stream, filters: _Filters, deadline: float,
    ) -> _Stream | SearchIssue:
        try:
            async with asyncio.timeout_at(deadline):
                async with self._requests:
                    try:
                        async with asyncio.timeout(REQUEST_TIMEOUT):
                            page = await self._backend.search_rich(
                                stream.chat_id, query=stream.query, author=filters.author,
                                since=filters.since, until=filters.until, limit=min(filters.limit, 8),
                                offset_id=stream.offset, after_message_id=filters.after_message_id,
                            )
                    except TimeoutError:
                        return SearchIssue(stream.chat, stream.query, "timeout", "Telegram search timed out.")
        except TimeoutError:
            # The page's time budget elapsed. Keep the unadvanced stream for the next call.
            return stream
        except TelegramSearchError as exc:
            return SearchIssue(stream.chat, stream.query, exc.code, str(exc), exc.retry_after_seconds)

        hits = {
            hit.message_id: hit for hit in page.hits
            if stream.offset is None or hit.message_id < stream.offset
        }
        ordered = tuple(sorted(hits.values(), key=_message_order, reverse=True))
        if ordered and stream.oldest is not None and _message_order(ordered[0]) > stream.oldest:
            return SearchIssue(stream.chat, stream.query, "unstable_order", "Telegram returned newer results after the cursor.")
        if page.next_offset_id is not None and stream.offset is not None and page.next_offset_id >= stream.offset:
            return SearchIssue(stream.chat, stream.query, "stalled_offset", "Telegram search stopped advancing.")
        return replace(
            stream, offset=page.next_offset_id, hits=ordered, exhausted=page.next_offset_id is None,
            oldest=_message_order(ordered[-1]) if ordered else stream.oldest,
        )

    async def _page(
        self, filters: _Filters, streams: tuple[_Stream, ...], earlier_errors: tuple[SearchIssue, ...] = (),
    ) -> MultiSearchResult:
        active = dict(enumerate(streams))
        failed: list[_Stream] = []
        errors = list(earlier_errors)
        hits: list[MultiSearchHit] = []
        budget = len(streams) + 16
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PAGE_TIMEOUT
        while active and len(hits) < filters.limit:
            empty = [index for index, stream in active.items() if not stream.hits]
            if empty:
                if budget <= 0 or loop.time() >= deadline:
                    break
                selected = empty[:budget]
                budget -= len(selected)
                results = await asyncio.gather(*(self._fetch(active[index], filters, deadline) for index in selected))
                for index, result in zip(selected, results, strict=True):
                    if isinstance(result, SearchIssue):
                        failed.append(active.pop(index))
                        errors.append(result)
                    elif result.exhausted and not result.hits:
                        del active[index]
                    else:
                        active[index] = result
                continue

            # Every active stream now has a head. Collect its query before advancing it.
            winner = max(active.values(), key=lambda stream: (stream.hits[0].date, stream.chat, stream.hits[0].message_id))
            message = winner.hits[0]
            matches: dict[str, str] = {}
            for index, stream in list(active.items()):
                head = stream.hits[0]
                if stream.chat_id != winner.chat_id or head.message_id != message.message_id:
                    continue
                matches[stream.query] = _match_source(head, stream.query)
                if len(stream.hits) == 1 and stream.exhausted:
                    del active[index]
                else:
                    active[index] = replace(stream, hits=stream.hits[1:])
            hits.append(MultiSearchHit(**asdict(message), chat=winner.chat, matched_queries=list(matches), match_sources=matches))

        remaining = tuple(active.values())
        error_snapshot = tuple(errors)
        retry = tuple(failed)
        return MultiSearchResult(
            hits=hits,
            errors=errors,
            partial=bool(errors),
            next_cursor=self._pages.add(lambda: self._page(filters, remaining, error_snapshot)) if remaining else None,
            retry_cursor=self._pages.add(lambda: self._page(filters, retry)) if retry else None,
        )
