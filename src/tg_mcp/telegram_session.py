import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from math import ceil
from pathlib import Path
from time import monotonic

from telethon import TelegramClient, errors, types

from .config import ConfigurationError, TelegramSettings
from .diagnostics import log_rpc


class SessionClient(TelegramClient):
    """Honor method-specific flood waits across callers sharing this session."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cooldowns: dict[int, float] = {}

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        batch = request if isinstance(request, (list, tuple)) else (request,)
        method = ",".join(type(item).__name__ for item in batch)
        start = monotonic()
        for item in batch:
            wait = ceil(self._cooldowns.get(item.CONSTRUCTOR_ID, 0) - start)
            if wait > 0:
                log_rpc(method, start, "cooldown", wait)
                raise errors.FloodWaitError(request=item, capture=wait)
        status = "error"
        wait = 0
        try:
            result = await super().__call__(request, ordered, flood_sleep_threshold)
            status = "ok"
            return result
        except errors.FloodWaitError as exc:
            status = "rate_limited"
            # With receive_updates=False Telethon may wrap the request.
            failed = getattr(exc.request, "query", exc.request)
            key = getattr(failed, "CONSTRUCTOR_ID", None)
            if key is None and len(batch) == 1:
                key = batch[0].CONSTRUCTOR_ID
            now = monotonic()
            wait = max(1, exc.seconds)
            if key is not None:
                due = max(self._cooldowns.get(key, 0), now + wait)
                self._cooldowns[key] = due
                wait = ceil(due - now)
            if exc.seconds != wait:
                raise errors.FloodWaitError(request=exc.request, capture=wait) from None
            raise
        finally:
            log_rpc(method, start, status, wait)


@contextmanager
def _hold_session_lock(session_path: Path) -> Iterator[None]:
    lock_path = session_path.with_suffix(".session.lock")
    try:
        if lock_path.resolve() != lock_path:
            raise ConfigurationError("The Telegram session lock must be a local file.")
        lock = lock_path.open("a+b")
    except (OSError, RuntimeError):
        raise ConfigurationError("Cannot open the Telegram session lock.") from None
    with lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ConfigurationError("This Telegram session is already in use by another process.") from None
        yield


@asynccontextmanager
async def open_session(
    settings: TelegramSettings, *, create: bool = False, for_search: bool = False,
) -> AsyncIterator[TelegramClient]:
    path = settings.session_path
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise ConfigurationError("Telegram session is missing. Run tg-mcp-setup login.")
    with _hold_session_lock(path):
        client_type = SessionClient if for_search else TelegramClient
        client = client_type(
            str(path), settings.api_id, settings.api_hash, receive_updates=False,
            flood_sleep_threshold=0, connection_retries=1, request_retries=1, timeout=10,
        )
        try:
            await client.connect()
            yield client
        finally:
            await client.disconnect()


@asynccontextmanager
async def open_user_session(settings: TelegramSettings) -> AsyncIterator[TelegramClient]:
    async with AsyncExitStack() as stack:
        # Bound startup while keeping the session locked until the server stops.
        async with asyncio.timeout(15):
            try:
                client = await stack.enter_async_context(open_session(settings, for_search=True))
                user = await client.get_me()
            except ConfigurationError:
                raise
            except errors.FloodWaitError as exc:
                raise ConfigurationError(f"Telegram asks you to wait {exc.seconds} seconds.") from None
            except (errors.RPCError, OSError, sqlite3.Error, ValueError):
                raise ConfigurationError("Cannot open the Telegram session. Check your connection and session.") from None
        if not isinstance(user, types.User) or user.bot:
            raise ConfigurationError("A Telegram user session is required. Run tg-mcp-setup login.")
        yield client
