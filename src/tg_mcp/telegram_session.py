import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from pathlib import Path

from telethon import TelegramClient, errors, types

from .config import ConfigurationError, TelegramSettings


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
    settings: TelegramSettings, *, create: bool = False,
) -> AsyncIterator[TelegramClient]:
    path = settings.session_path
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise ConfigurationError("Telegram session is missing. Run tg-mcp-setup login.")
    with _hold_session_lock(path):
        client = TelegramClient(
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
                client = await stack.enter_async_context(open_session(settings))
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
