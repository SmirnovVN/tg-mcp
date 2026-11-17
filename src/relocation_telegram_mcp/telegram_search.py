import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from pathlib import Path

from telethon import TelegramClient, errors, functions, types, utils

ChatPeer = types.InputPeerChat | types.InputPeerChannel


class TelegramSearchError(RuntimeError):
    """A Telegram error safe to return to the MCP client."""


@dataclass(frozen=True)
class SearchHit:
    message_id: int
    date: datetime
    sender_id: int | None
    text: str
    url: str | None


@dataclass(frozen=True)
class SearchPage:
    hits: list[SearchHit]
    next_offset_id: int | None


@dataclass(frozen=True)
class MessageContext:
    message: SearchHit
    before: list[SearchHit]
    after: list[SearchHit]
    replied_to: SearchHit | None


class TelegramSearch:
    def __init__(self, session_path: Path, api_id: int, api_hash: str) -> None:
        self._session_path = session_path
        self._api_id = api_id
        self._api_hash = api_hash
        self._client: TelegramClient | None = None

    async def start(self) -> None:
        if self._session_path.suffix != ".session" or not self._session_path.is_file():
            raise TelegramSearchError("Telegram session is missing. Run tg-mcp-setup --config config.toml login.")
        try:
            self._client = TelegramClient(
                str(self._session_path), self._api_id, self._api_hash,
                receive_updates=False, flood_sleep_threshold=0,
                connection_retries=1, request_retries=1, timeout=10,
            )
            await self._client.connect()
            user = await self._client.get_me()
        except errors.FloodWaitError as exc:
            raise TelegramSearchError(f"Telegram asks you to wait {exc.seconds} seconds.") from None
        except (errors.RPCError, OSError, sqlite3.Error, ValueError):
            raise TelegramSearchError("Cannot open the Telegram session. Check your connection and session.") from None
        if not isinstance(user, types.User) or user.bot:
            raise TelegramSearchError("A Telegram user session is required. Run tg-mcp-setup login.")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    @asynccontextmanager
    async def _open_chat(self, chat_id: int) -> AsyncIterator[tuple[TelegramClient, ChatPeer]]:
        client = self._client
        if client is None:
            raise TelegramSearchError("The Telegram session is unavailable.")
        try:
            try:
                peer = await client.get_input_entity(chat_id)
            except ValueError:
                raise TelegramSearchError(
                    "Chat is unavailable. Run tg-mcp-setup --config config.toml chats to refresh its ID."
                ) from None
            if not isinstance(peer, (types.InputPeerChat, types.InputPeerChannel)) or utils.get_peer_id(peer) != chat_id:
                raise TelegramSearchError("The configured ID must resolve to the intended group or channel.")

            yield client, peer
        except errors.FloodWaitError as exc:
            raise TelegramSearchError(f"Telegram asks you to wait {exc.seconds} seconds before retrying.") from None
        except (errors.ChannelPrivateError, errors.ChannelInvalidError, errors.ChatIdInvalidError):
            raise TelegramSearchError("The configured chat is unavailable to this session.") from None
        except TimeoutError:
            raise
        except (errors.RPCError, OSError, ValueError, sqlite3.Error):
            raise TelegramSearchError("Telegram request failed. Check chat access and the connection.") from None

    async def search(
        self,
        chat_id: int,
        *,
        query: str | None,
        author: int | str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        offset_id: int | None,
    ) -> SearchPage:
        async with self._open_chat(chat_id) as (client, peer):
            sender = await _resolve_author(client, author)
            return await _search_pages(client, peer, query, sender, since, until, limit, offset_id)

    async def get_context(self, chat_id: int, message_id: int, *, before: int, after: int) -> MessageContext:
        async with self._open_chat(chat_id) as (client, peer):
            # The history offset counts the anchor itself. Move back one extra slot
            # so the window contains the anchor as well as `after` newer messages.
            rows = await _get_history(
                client, peer, offset_id=message_id, add_offset=-(after + 1), limit=before + after + 1,
            )
            messages = {
                row.id: row for row in rows
                if isinstance(row, types.Message) and utils.get_peer_id(row.peer_id) == chat_id
            }
            anchor = messages.get(message_id)
            if anchor is None:
                raise TelegramSearchError("The message is unavailable in the configured chat.")

            parent = None
            reply = anchor.reply_to
            if (
                isinstance(reply, types.MessageReplyHeader) and reply.reply_to_msg_id
                and (reply.reply_to_peer_id is None or utils.get_peer_id(reply.reply_to_peer_id) == chat_id)
            ):
                parent = messages.get(reply.reply_to_msg_id)
                if parent is None:
                    reply_rows = await _get_history(
                        client, peer, offset_id=reply.reply_to_msg_id, add_offset=-1, limit=1,
                    )
                    parent = next((
                        row for row in reply_rows
                        if isinstance(row, types.Message) and row.id == reply.reply_to_msg_id
                        and utils.get_peer_id(row.peer_id) == chat_id
                    ), None)

            older = sorted(mid for mid in messages if mid < message_id)
            newer = sorted(mid for mid in messages if mid > message_id)
            return MessageContext(
                message=_message_hit(anchor, peer),
                before=[_message_hit(messages[mid], peer) for mid in older[-before:]] if before else [],
                after=[_message_hit(messages[mid], peer) for mid in newer[:after]],
                replied_to=_message_hit(parent, peer) if parent is not None else None,
            )


async def _get_history(
    client: TelegramClient,
    peer: ChatPeer,
    *,
    offset_id: int = 0,
    offset_date: int | None = None,
    add_offset: int = 0,
    limit: int,
) -> list[types.TypeMessage]:
    result = await client(functions.messages.GetHistoryRequest(
        peer=peer, offset_id=offset_id, offset_date=offset_date, add_offset=add_offset,
        limit=limit, max_id=0, min_id=0, hash=0,
    ))
    return result.messages


def _message_hit(message: types.Message, peer: ChatPeer) -> SearchHit:
    sender_id = utils.get_peer_id(message.from_id) if message.from_id is not None else None
    url = f"https://t.me/c/{peer.channel_id}/{message.id}" if isinstance(peer, types.InputPeerChannel) else None
    return SearchHit(message.id, message.date, sender_id, message.message or "", url)


async def _resolve_author(
    client: TelegramClient, author: int | str | None,
) -> types.InputPeerUser | None:
    if author is None:
        return None
    try:
        if isinstance(author, str):
            user = await client.get_entity(author)
            if not isinstance(user, types.User):
                raise ValueError
            sender = utils.get_input_peer(user, allow_self=False)
        else:
            sender = await client.get_input_entity(types.PeerUser(author))
            if isinstance(sender, types.InputPeerSelf):
                sender = await client.get_me(input_peer=True)
    except (ValueError, TypeError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
        raise TelegramSearchError("Author is unavailable. Try a current @username or a known sender_id.") from None
    if not isinstance(sender, types.InputPeerUser) or (
        isinstance(author, int) and sender.user_id != author
    ):
        raise TelegramSearchError("Author must resolve to a Telegram user.")
    return sender


async def _search_pages(
    client: TelegramClient,
    peer: ChatPeer,
    query: str | None,
    sender: types.InputPeerUser | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    offset_id: int | None,
) -> SearchPage:
    offset = offset_id or 0
    if not offset and until is not None:
        # GetHistory applies the upper date bound before text search, then older pages
        # can use the stable message ID cursor without scanning newer hits again.
        timestamp = ceil(until.timestamp())
        if timestamp <= 0:
            return SearchPage([], None)
        if timestamp < 2**31:
            anchor = await _get_history(client, peer, offset_date=timestamp, limit=1)
            if not anchor:
                return SearchPage([], None)
            offset = min(anchor[0].id + 1, 2**31 - 1)

    hits: list[SearchHit] = []
    chat_id = utils.get_peer_id(peer)
    for _ in range(4):
        result = await client(functions.messages.SearchRequest(
            peer=peer, q=query or "", from_id=sender, filter=types.InputMessagesFilterEmpty(),
            min_date=None, max_date=None, offset_id=offset, add_offset=0,
            limit=min(limit + 1, 51), max_id=0, min_id=0, hash=0,
        ))
        rows = sorted(
            (row for row in result.messages if row.id > 0 and (offset == 0 or row.id < offset)),
            key=lambda row: row.id, reverse=True,
        )
        if not rows:
            return SearchPage(hits, None)
        for row in rows:
            if not isinstance(row, (types.Message, types.MessageService)) or utils.get_peer_id(row.peer_id) != chat_id:
                continue
            if since is not None and row.date < since:
                return SearchPage(hits, None)
            if not isinstance(row, types.Message) or (until is not None and row.date >= until):
                continue
            hit = _message_hit(row, peer)
            if sender is not None and hit.sender_id != sender.user_id:
                continue
            # IDs advance across batches, but Telegram can repeat an ID within one batch.
            if hits and hit.message_id == hits[-1].message_id:
                continue
            if len(hits) == limit:
                return SearchPage(hits, hits[-1].message_id)
            hits.append(hit)
        offset = rows[-1].id

    # A short page can contain unavailable messages. Keep the cursor so the
    # caller can continue after this call's fixed batch budget.
    return SearchPage(hits, offset)
