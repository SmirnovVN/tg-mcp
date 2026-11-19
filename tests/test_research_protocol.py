from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from telethon import errors, functions, types, utils

from tg_mcp.telegram_search import TelegramSearch, TelegramSearchError


class ResearchProtocolTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.peer = types.InputPeerChannel(1234567890, 15)
        self.chat_id = utils.get_peer_id(self.peer)
        self.client = AsyncMock()
        self.client.get_input_entity.return_value = self.peer
        self.backend = TelegramSearch(self.client)

    def message(self, message_id: int, *, parent: int | None = None, text: str = "test", **kwargs):
        return types.Message(
            id=message_id, peer_id=types.PeerChannel(self.peer.channel_id),
            date=datetime(2026, 3, 15, tzinfo=UTC), message=text,
            from_id=types.PeerUser(12345),
            reply_to=types.MessageReplyHeader(reply_to_msg_id=parent) if parent else None,
            **kwargs,
        )

    async def test_rich_results_distinguish_caption_and_absent_voice_text(self) -> None:
        voice = types.Document(
            id=1, access_hash=1, file_reference=b"", date=datetime(2026, 3, 15, tzinfo=UTC),
            mime_type="audio/ogg", size=100, dc_id=2,
            attributes=[types.DocumentAttributeAudio(duration=10, voice=True)],
        )
        photo = self.message(90, text="photo caption", media=types.MessageMediaPhoto(photo=types.PhotoEmpty(1)))
        audio = self.message(80, text=" ", media=types.MessageMediaDocument(document=voice))
        preview = self.message(70, text="link", media=types.MessageMediaWebPage(webpage=types.WebPageEmpty(1)))
        self.client.side_effect = [SimpleNamespace(messages=[photo, audio, preview]), SimpleNamespace(messages=[])]

        result = await self.backend.search_rich(
            self.chat_id, query="caption", author=None, since=None, until=None, limit=5, offset_id=None,
        )

        first, second, third = result.hits
        self.assertIsNone(first.text)
        self.assertEqual((first.caption, first.message_type, first.text_status), ("photo caption", "photo", "caption"))
        self.assertIsNone(second.text)
        self.assertIsNone(second.caption)
        self.assertEqual((second.message_type, second.text_status), ("voice", "absent"))
        self.assertEqual((third.message_type, third.text, third.caption), ("link_preview", "link", None))
        self.assertIsNone(result.next_offset_id)

    async def test_rich_search_bounds_followups_after_the_anchor(self) -> None:
        author = types.InputPeerUser(12345, 8)
        self.client.get_input_entity.side_effect = [self.peer, author]
        self.client.return_value = SimpleNamespace(messages=[self.message(90), self.message(80), self.message(50)])

        result = await self.backend.search_rich(
            self.chat_id, query="", author=12345, since=None, until=None,
            limit=10, offset_id=None, after_message_id=80,
        )

        self.assertEqual([message.message_id for message in result.hits], [90])
        self.assertIsNone(result.next_offset_id)
        request = self.client.await_args.args[0]
        self.assertEqual(request.min_id, 80)
        self.assertEqual(request.from_id, author)

    async def test_anchor_does_not_load_a_parent_from_another_chat(self) -> None:
        anchor = self.message(90)
        anchor.reply_to = types.MessageReplyHeader(
            reply_to_msg_id=70, reply_to_peer_id=types.PeerChannel(987654321),
        )
        self.client.return_value = SimpleNamespace(messages=[anchor])

        result = await self.backend.get_story_anchor(self.chat_id, 90)

        self.assertEqual(result.parent_status, "outside_chat")
        self.assertIsNone(result.replied_to)
        self.assertTrue(result.is_user_author)
        self.client.assert_awaited_once()

    async def test_anchor_reports_a_deleted_parent(self) -> None:
        self.client.side_effect = [
            SimpleNamespace(messages=[self.message(90, parent=70)]),
            SimpleNamespace(messages=[types.MessageEmpty(id=70, peer_id=None)]),
        ]
        result = await self.backend.get_story_anchor(self.chat_id, 90)
        self.assertEqual(result.parent_status, "unavailable")
        self.assertIsNone(result.replied_to)

    async def test_broadcast_anchor_selects_history_before_getting_replies(self) -> None:
        anchor = self.message(50, post=True)
        self.client.side_effect = [
            SimpleNamespace(messages=[anchor]),
            SimpleNamespace(messages=[self.message(90, parent=50), anchor]),
        ]
        story = await self.backend.get_story_anchor(self.chat_id, 50)
        result = await self.backend.get_story_replies(
            self.chat_id, 50, limit=5, offset_id=None, use_history=story.use_history,
        )
        self.assertTrue(story.use_history)
        self.assertEqual([message.message_id for message in result.hits], [90])
        self.assertTrue(all(
            isinstance(call.args[0], functions.messages.GetHistoryRequest)
            for call in self.client.await_args_list
        ))

    async def test_reply_pages_only_include_direct_same_chat_reply_headers(self) -> None:
        cross_chat = self.message(110, parent=50)
        cross_chat.reply_to.reply_to_peer_id = types.PeerChannel(987654321)
        nested = self.message(100, parent=70)
        nested.reply_to.reply_to_top_id = 50
        self.client.side_effect = [
            SimpleNamespace(messages=[cross_chat, nested, self.message(90, parent=50), self.message(70, parent=50)]),
        ]
        result = await self.backend.get_story_replies(self.chat_id, 50, limit=1, offset_id=None)
        self.assertEqual([message.message_id for message in result.hits], [90])
        self.assertEqual(result.next_offset_id, 90)
        self.assertEqual(result.status, "more")
        self.assertFalse(result.use_history)
        self.assertIsInstance(self.client.await_args.args[0], functions.messages.GetRepliesRequest)

    async def test_unsupported_replies_scan_bounded_history_and_keep_sparse_cursor(self) -> None:
        self.client.side_effect = [
            errors.PeerIdInvalidError(request=None),
            SimpleNamespace(messages=[self.message(300), self.message(299)]),
            SimpleNamespace(messages=[types.MessageEmpty(id=250, peer_id=None)]),
            SimpleNamespace(messages=[self.message(200)]),
            SimpleNamespace(messages=[self.message(150)]),
        ]
        result = await self.backend.get_story_replies(self.chat_id, 50, limit=5, offset_id=None)

        self.assertEqual(result.hits, [])
        self.assertEqual(result.next_offset_id, 150)
        self.assertEqual(result.status, "more")
        self.assertTrue(result.use_history)
        requests = [call.args[0] for call in self.client.await_args_list]
        self.assertTrue(all(isinstance(request, functions.messages.GetHistoryRequest) for request in requests[1:]))
        self.assertEqual([request.offset_id for request in requests[1:]], [0, 299, 250, 200])

    async def test_linked_discussion_rows_never_escape_configured_chat(self) -> None:
        foreign = self.message(5000, parent=50)
        foreign.peer_id = types.PeerChannel(987654321)
        self.client.side_effect = [
            SimpleNamespace(messages=[foreign]),
            SimpleNamespace(messages=[self.message(90, parent=50), self.message(50)]),
        ]
        result = await self.backend.get_story_replies(self.chat_id, 50, limit=5, offset_id=None)

        self.assertEqual([message.message_id for message in result.hits], [90])
        self.assertIsNone(result.next_offset_id)
        self.assertTrue(result.use_history)
        history = self.client.await_args_list[1].args[0]
        self.assertEqual(history.peer, self.peer)
        self.assertEqual(history.offset_id, 0)

    async def test_forum_reply_lookup_falls_back_to_local_history(self) -> None:
        self.client.side_effect = [
            errors.BadRequestError(request=None, message="TOPIC_ID_INVALID", code=400),
            SimpleNamespace(messages=[self.message(90, parent=50), self.message(50)]),
        ]

        result = await self.backend.get_story_replies(self.chat_id, 50, limit=5, offset_id=None)

        self.assertTrue(result.use_history)
        self.assertEqual([message.message_id for message in result.hits], [90])
        self.assertIsNone(result.next_offset_id)

    async def test_rate_limit_exposes_only_safe_retry_details(self) -> None:
        self.client.side_effect = errors.FloodWaitError(request=None, capture=32)
        with self.assertRaises(TelegramSearchError) as raised:
            await self.backend.get_story_anchor(self.chat_id, 90)
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.retry_after_seconds, 32)
