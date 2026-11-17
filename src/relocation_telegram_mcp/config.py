import os
import re
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    """A configuration problem safe to show to the caller."""


@dataclass(frozen=True)
class TelegramSettings:
    api_id: int
    api_hash: str = field(repr=False)
    session_path: Path


@dataclass(frozen=True)
class Settings(TelegramSettings):
    chats: dict[str, int]


def _load_config(path: Path) -> tuple[TelegramSettings, dict]:
    try:
        path = path.resolve(strict=True)
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError("Cannot read the local TOML configuration.") from None
    if data.keys() - {"telegram", "chats"}:
        raise ConfigurationError("Use only [telegram] and [chats] in the configuration.")

    telegram = data.get("telegram")
    if not isinstance(telegram, dict) or telegram.keys() != {"api_id", "api_hash", "session"}:
        raise ConfigurationError("Set api_id, api_hash and session in [telegram].")
    api_id = telegram["api_id"]
    api_hash = telegram["api_hash"]
    if type(api_id) is not int or not 0 < api_id < 2**31:
        raise ConfigurationError("[telegram].api_id must be a positive Telegram API ID.")
    if not isinstance(api_hash, str) or not api_hash.strip():
        raise ConfigurationError("Set [telegram].api_hash from Telegram API development tools.")

    session = telegram["session"]
    if not isinstance(session, str) or not session.strip():
        raise ConfigurationError("[telegram].session must be a relative .session path.")
    relative = Path(session)
    if relative.anchor or relative.suffix != ".session":
        raise ConfigurationError("[telegram].session must be a relative .session path.")
    try:
        session_path = (path.parent / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError("Cannot resolve the Telegram session path.") from None
    if not session_path.is_relative_to(path.parent) or session_path.suffix != ".session":
        raise ConfigurationError("The session must stay in the configuration directory.")
    return TelegramSettings(api_id, api_hash.strip(), session_path), data


def load_setup_settings(path: Path) -> TelegramSettings:
    settings, _ = _load_config(path)
    return settings


def load_settings(path: Path) -> Settings:
    settings, data = _load_config(path)
    chats = data.get("chats")
    if not isinstance(chats, dict) or not chats:
        raise ConfigurationError("Configure at least one allowed chat in [chats].")
    used_ids = set()
    for alias, chat_id in chats.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias):
            raise ConfigurationError("Use 1–64 ASCII letters, digits, underscores or hyphens for chat aliases.")
        if type(chat_id) is not int or not -(2**63) < chat_id < 0:
            raise ConfigurationError("Each configured chat must have a negative Telegram group or channel ID.")
        if chat_id in used_ids:
            raise ConfigurationError("Configure each Telegram chat ID only once.")
        used_ids.add(chat_id)
    return Settings(settings.api_id, settings.api_hash, settings.session_path, chats)


@contextmanager
def hold_session_lock(session_path: Path) -> Iterator[None]:
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
