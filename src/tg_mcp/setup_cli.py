import argparse
import asyncio
import re
import sqlite3
import sys
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient, errors

from .config import ConfigurationError, TelegramSettings, load_setup_settings
from .telegram_session import open_session


async def _login(client: TelegramClient) -> None:
    if await client.is_user_authorized():
        print("The Telegram session is already authorized.")
        return
    if not sys.stdin.isatty():
        raise ConfigurationError("Run login in an interactive terminal.")
    phone = input("Telegram phone number with country code: ").strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        raise ConfigurationError("Enter a phone number in international format, such as +1234567890.")
    await client.send_code_request(phone)
    code = getpass("Telegram login code: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except errors.SessionPasswordNeededError:
        await client.sign_in(password=getpass("Telegram two-step verification password: "))
    print("Telegram session saved.")


async def _list_chats(client: TelegramClient, configured_chats: dict[str, int] | None = None) -> None:
    if not await client.is_user_authorized():
        raise ConfigurationError("Run tg-mcp-setup --config config.toml login first.")
    dialogs = (
        dialog async for dialog in client.iter_dialogs()
        if dialog.id < 0 and (dialog.is_group or dialog.is_channel)
    )
    if configured_chats is None:
        print("Chat ID\tTitle")
        async for dialog in dialogs:
            print(f"{dialog.id}\t{dialog.name!r}")
        return

    remaining = {dialog.id: dialog async for dialog in dialogs}
    lines = ["[chats]"]
    used_aliases = set(configured_chats)
    for alias, chat_id in configured_chats.items():
        dialog = remaining.pop(chat_id, None)
        comment = f"  # {dialog.name!r}" if dialog is not None else ""
        lines.append(f"{alias} = {chat_id}{comment}")

    for chat_id, dialog in remaining.items():
        username = getattr(dialog.entity, "username", None)
        base = f"chat_{-chat_id}"
        if username and re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
            base = username.lower()
        alias = base
        suffix = 2
        while alias in used_aliases:
            alias = f"{base}_{suffix}"
            suffix += 1
        used_aliases.add(alias)
        lines.append(f"{alias} = {chat_id}  # {dialog.name!r}")
    print("\n".join(lines))


async def _run(
    settings: TelegramSettings, command: str, configured_chats: dict[str, int] | None = None,
) -> None:
    async with open_session(settings, create=command == "login") as client:
        if command == "login":
            await _login(client)
        else:
            await _list_chats(client, configured_chats)


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorize a local Telegram session and choose chat IDs.")
    parser.add_argument("--config", required=True, type=Path, help="Path to the local TOML configuration.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Authorize the session interactively.").set_defaults(toml=False)
    chats = commands.add_parser("chats", help="List your groups and channels in this terminal.")
    chats.add_argument("--toml", action="store_true", help="Print a [chats] section ready to paste into the config.")
    args = parser.parse_args()
    try:
        settings, configured_chats = load_setup_settings(args.config, include_chats=args.toml)
        asyncio.run(_run(settings, args.command, configured_chats))
    except ConfigurationError as exc:
        parser.exit(1, f"tg-mcp-setup: {exc}\n")
    except errors.FloodWaitError as exc:
        parser.exit(1, f"tg-mcp-setup: Telegram asks you to wait {exc.seconds} seconds.\n")
    except errors.PhoneCodeInvalidError:
        parser.exit(1, "tg-mcp-setup: Incorrect login code. Run login again.\n")
    except errors.PasswordHashInvalidError:
        parser.exit(1, "tg-mcp-setup: Incorrect two-step verification password.\n")
    except sqlite3.Error:
        parser.exit(1, "tg-mcp-setup: Cannot read or write the Telegram session database.\n")
    except (errors.RPCError, OSError, ValueError):
        parser.exit(1, "tg-mcp-setup: Telegram setup failed. Check configuration and connection.\n")
    except (KeyboardInterrupt, EOFError):
        parser.exit(130, "\ntg-mcp-setup: Cancelled.\n")


if __name__ == "__main__":
    main()
