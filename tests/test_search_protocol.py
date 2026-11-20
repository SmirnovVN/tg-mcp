from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from telethon import functions, types, utils

from tg_mcp.telegram_search import TelegramSearch, TelegramSearchError


class SearchProtocolTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.peer = types.InputPeerChannel(1234567890, 15)
        self.author = types.InputPeerUser(12345, 16)
        self.chat_id = utils.get_peer_id(self.peer)
        self.client = AsyncMock()
        self.client.get_input_entity.side_effect = [self.peer, self.author, self.peer, self.author]
        self.search = TelegramSearch(self.client)

    def message(self, id: int, day: int, sender_id: int = 12345) -> types.Message:
        return types.Message(
            id=id, peer_id=types.PeerChannel(self.peer.channel_id),
            date=datetime(2026, 3, day, tzinfo=UTC), message="test",
            from_id=types.PeerUser(sender_id),
        )

    async def test_author_only_query_uses_date_anchor_and_exclusive_cursor(self) -> None:
        self.client.side_effect = [
            SimpleNamespace(messages=[self.message(90, 8)]),
            SimpleNamespace(messages=[self.message(90, 8), self.message(80, 7, sender_id=12456)]),
            SimpleNamespace(messages=[self.message(50, 2)]),
            SimpleNamespace(messages=[self.message(50, 2), self.message(20, 1)]),
        ]
        filters = dict(
            query=None, author=self.author.user_id, limit=1,
            since=datetime(2026, 3, 2, tzinfo=UTC), until=datetime(2026, 3, 9, tzinfo=UTC),
        )
        first = await self.search.search(self.chat_id, offset_id=None, **filters)
        second = await self.search.search(self.chat_id, offset_id=first.next_offset_id, **filters)

        self.assertEqual([hit.message_id for hit in first.hits], [90])
        self.assertEqual(first.next_offset_id, 90)
        self.assertEqual([hit.message_id for hit in second.hits], [50])
        self.assertEqual(second.hits[0].url, "https://t.me/c/1234567890/50")
        self.assertIsNone(second.next_offset_id)
        anchor, *requests = [call.args[0] for call in self.client.await_args_list]
        self.assertIsInstance(anchor, functions.messages.GetHistoryRequest)
        self.assertTrue(all(isinstance(request, functions.messages.SearchRequest) for request in requests))
        self.assertEqual([request.offset_id for request in requests], [91, 80, 90])
        self.assertTrue(all(request.from_id is self.author and request.q == "" for request in requests))

    async def test_sparse_results_keep_cursor_even_when_page_is_empty(self) -> None:
        self.client.side_effect = [
            SimpleNamespace(messages=[types.MessageEmpty(id=90, peer_id=None), types.MessageEmpty(id=89, peer_id=None)]),
            SimpleNamespace(messages=[types.MessageEmpty(id=70, peer_id=None)]),
            SimpleNamespace(messages=[types.MessageEmpty(id=60, peer_id=None)]),
            SimpleNamespace(messages=[types.MessageEmpty(id=50, peer_id=None)]),
        ]
        page = await self.search.search(
            self.chat_id, query="€500 deposit", author=None, since=None, until=None, limit=2, offset_id=None,
        )

        self.assertEqual(page.hits, [])
        self.assertEqual(page.next_offset_id, 50)
        requests = [call.args[0] for call in self.client.await_args_list]
        self.assertEqual([request.offset_id for request in requests], [0, 89, 70, 60])
        self.assertTrue(all(request.peer is self.peer and request.q == "€500 deposit" for request in requests))

    async def test_repeated_id_does_not_consume_a_page_slot(self) -> None:
        self.client.side_effect = [
            SimpleNamespace(messages=[self.message(90, 8), self.message(90, 8), self.message(80, 7)]),
            SimpleNamespace(messages=[self.message(70, 6)]),
        ]
        page = await self.search.search(
            self.chat_id, query="€500 deposit", author=None, since=None, until=None, limit=2, offset_id=None,
        )

        self.assertEqual([hit.message_id for hit in page.hits], [90, 80])
        self.assertEqual(page.next_offset_id, 80)

    async def test_search_accepts_the_signed_in_account_as_author(self) -> None:
        self.client.get_input_entity.side_effect = [self.peer, self.peer, types.InputPeerSelf()]
        self.client.get_entity.return_value = types.User(
            id=self.author.user_id, access_hash=self.author.access_hash, is_self=True,
        )
        self.client.get_me.return_value = self.author
        self.client.return_value = SimpleNamespace(messages=[])

        for author in ("@example", self.author.user_id):
            await self.search.search(
                self.chat_id, query=None, author=author, since=None, until=None, limit=2, offset_id=None,
            )

        actual = [call.args[0].from_id for call in self.client.await_args_list]
        self.assertEqual([peer.to_dict() for peer in actual], [self.author.to_dict()] * 2)
        self.client.get_me.assert_awaited_once_with(input_peer=True)

    async def test_context_keeps_nearby_messages_and_loads_distant_parent(self) -> None:
        anchor = self.message(90, 8)
        anchor.reply_to = types.MessageReplyHeader(reply_to_msg_id=20)
        self.client.side_effect = [
            SimpleNamespace(messages=[self.message(110, 10), self.message(95, 9), anchor, self.message(70, 6)]),
            SimpleNamespace(messages=[self.message(20, 1)]),
        ]

        context = await self.search.get_context(self.chat_id, 90, before=1, after=2)

        self.assertEqual(context.message.message_id, 90)
        self.assertEqual([hit.message_id for hit in context.before], [70])
        self.assertEqual([hit.message_id for hit in context.after], [95, 110])
        self.assertEqual(context.replied_to.message_id, 20)
        requests = [call.args[0] for call in self.client.await_args_list]
        self.assertTrue(all(isinstance(request, functions.messages.GetHistoryRequest) for request in requests))
        self.assertTrue(all(request.peer is self.peer for request in requests))
        self.assertEqual([(r.offset_id, r.add_offset, r.limit) for r in requests], [(90, -3, 4), (20, -1, 1)])

    async def test_context_does_not_follow_cross_chat_reply(self) -> None:
        anchor = self.message(90, 8)
        anchor.reply_to = types.MessageReplyHeader(
            reply_to_msg_id=70, reply_to_peer_id=types.PeerChannel(987654321),
        )
        self.client.return_value = SimpleNamespace(messages=[anchor, self.message(70, 6)])

        context = await self.search.get_context(self.chat_id, 90, before=1, after=0)

        self.assertIsNone(context.replied_to)
        self.assertEqual([hit.message_id for hit in context.before], [70])
        self.assertEqual(context.after, [])
        self.client.assert_awaited_once()

    async def test_context_rejects_anchor_from_another_chat(self) -> None:
        foreign = self.message(90, 8)
        foreign.peer_id = types.PeerChannel(987654321)
        self.client.return_value = SimpleNamespace(messages=[foreign, self.message(70, 6)])

        with self.assertRaises(TelegramSearchError):
            await self.search.get_context(self.chat_id, 90, before=1, after=0)
