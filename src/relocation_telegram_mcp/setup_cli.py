import argparse
import asyncio
import sqlite3
import sys
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient, errors

from .config import ConfigurationError, TelegramSettings, hold_session_lock, load_setup_settings


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


async def _list_chats(client: TelegramClient) -> None:
    if not await client.is_user_authorized():
        raise ConfigurationError("Run tg-mcp-setup --config config.toml login first.")
    print("Chat ID\tTitle")
    async for dialog in client.iter_dialogs():
        if dialog.id < 0 and (dialog.is_group or dialog.is_channel):
            print(f"{dialog.id}\t{dialog.name!r}")


async def _run(settings: TelegramSettings, command: str) -> None:
    if command == "login":
        settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    elif not settings.session_path.is_file():
        raise ConfigurationError("Run tg-mcp-setup --config config.toml login first.")
    with hold_session_lock(settings.session_path):
        client = TelegramClient(
            str(settings.session_path), settings.api_id, settings.api_hash, receive_updates=False,
            flood_sleep_threshold=0, connection_retries=1, request_retries=1,
        )
        try:
            await client.connect()
            if command == "login":
                await _login(client)
            else:
                await _list_chats(client)
        finally:
            await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorize a local Telegram session and choose chat IDs.")
    parser.add_argument("--config", required=True, type=Path, help="Path to the local TOML configuration.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Authorize the session interactively.")
    commands.add_parser("chats", help="List your groups and channels in this terminal.")
    args = parser.parse_args()
    try:
        settings = load_setup_settings(args.config)
        asyncio.run(_run(settings, args.command))
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
