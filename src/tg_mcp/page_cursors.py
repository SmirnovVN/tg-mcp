import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from secrets import token_urlsafe
from time import monotonic
from typing import Generic, TypeVar

Page = TypeVar("Page")


class CursorExpired(ValueError):
    def __init__(self) -> None:
        super().__init__("cursor_expired: Start a new request to recover this continuation.")


@dataclass
class _Entry(Generic[Page]):
    expires_at: float
    load: Callable[[], Awaitable[Page]] | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    result: Page | None = None


class CursorPages(Generic[Page]):
    """Bounded, replayable continuations shared by the local MCP process."""

    def __init__(self, *, capacity: int = 128, ttl: float = 1800) -> None:
        self._entries: OrderedDict[str, _Entry[Page]] = OrderedDict()
        self._capacity = capacity
        self._ttl = ttl

    def add(self, load: Callable[[], Awaitable[Page]]) -> str:
        now = monotonic()
        for cursor in [key for key, entry in self._entries.items() if entry.expires_at <= now]:
            del self._entries[cursor]
        while len(self._entries) >= self._capacity:
            self._entries.popitem(last=False)
        cursor = token_urlsafe(24)
        self._entries[cursor] = _Entry(now + self._ttl, load)
        return cursor

    async def get(self, cursor: str) -> Page:
        entry = self._entries.get(cursor)
        if entry is None or entry.expires_at <= monotonic():
            self._entries.pop(cursor, None)
            raise CursorExpired()
        self._entries.move_to_end(cursor)
        async with entry.lock:
            if entry.load is not None:
                entry.result = await entry.load()
                entry.load = None
            assert entry.result is not None
            return entry.result
