import io
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient

from tg_mcp.config import load_settings
from tg_mcp.setup_cli import main


class SetupCliTests(TestCase):
    def test_chats_toml_preserves_aliases_and_can_be_pasted_into_config(self) -> None:
        telegram = '[telegram]\napi_id = 12345\napi_hash = "test"\nsession = "test.session"\n'
        client = AsyncMock(spec=TelegramClient)
        client.is_user_authorized.return_value = True
        client.disconnect = AsyncMock()

        async def dialogs():
            for chat_id, name, username, is_group in (
                (-202, "Housing\n[telegram]", "Housing", True),
                (-101, "Saved group", "Renamed", True),
                (-303, "Private group", None, True),
                (404, "Private message", "Contact", False),
            ):
                yield SimpleNamespace(
                    id=chat_id, name=name, entity=SimpleNamespace(username=username),
                    is_group=is_group, is_channel=False,
                )

        client.iter_dialogs.side_effect = dialogs
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(dir=project) as directory:
            config = Path(directory) / "config.toml"
            config.with_name("test.session").touch()
            config.write_text(telegram + "\n[chats]\nhousing = -101\narchive = -505\n", encoding="utf-8")
            with (
                patch("sys.argv", ["tg-mcp-setup", "--config", str(config), "chats", "--toml"]),
                patch("tg_mcp.setup_cli.TelegramClient", return_value=client),
                redirect_stdout(io.StringIO()) as output,
            ):
                main()

            export = output.getvalue()
            expected = {"chats": {"housing": -101, "archive": -505, "housing_2": -202, "chat_303": -303}}
            self.assertEqual(tomllib.loads(export), expected)
            self.assertIn("# 'Housing\\n[telegram]'", export)
            config.write_text(telegram + "\n" + export, encoding="utf-8")
            self.assertEqual(load_settings(config).chats, expected["chats"])
