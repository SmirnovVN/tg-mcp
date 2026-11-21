import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from math import ceil

from telethon import TelegramClient, errors, functions, types, utils

ChatPeer = types.InputPeerChat | types.InputPeerChannel


class TelegramSearchError(RuntimeError):
    """A Telegram error safe to return to the MCP client."""

    def __init__(
        self, message: str, *, code: str = "telegram_error", retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.retry_cursor: str | None = None

    @property
    def retryable(self) -> bool:
        return self.code in {"rate_limited", "timeout", "telegram_error"}


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


@dataclass(frozen=True)
class RichMessage:
    message_id: int
    date: datetime
    sender_id: int | None
    text: str | None
    caption: str | None
    message_type: str
    text_status: str
    url: str | None


@dataclass(frozen=True)
class RichSearchPage:
    hits: list[RichMessage]
    next_offset_id: int | None


@dataclass(frozen=True)
class StoryAnchor:
    message: RichMessage
    replied_to: RichMessage | None
    parent_status: str
    is_user_author: bool
    use_history: bool = False
    parent_message_id: int | None = None


@dataclass(frozen=True)
class ReplyPage:
    hits: list[RichMessage]
    next_offset_id: int | None
    use_history: bool
    status: str
    rows_scanned: int = 0


class TelegramSearch:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    @asynccontextmanager
    async def _open_chat(self, chat_id: int) -> AsyncIterator[ChatPeer]:
        try:
            try:
                peer = await self._client.get_input_entity(chat_id)
            except ValueError:
                raise TelegramSearchError(
                    "Chat is unavailable. Run tg-mcp-setup chats to refresh its ID.",
                    code="chat_unavailable",
                ) from None
            if not isinstance(peer, (types.InputPeerChat, types.InputPeerChannel)) or utils.get_peer_id(peer) != chat_id:
                raise TelegramSearchError("The configured ID must resolve to the intended group or channel.")

            yield peer
        except errors.FloodWaitError as exc:
            raise TelegramSearchError(
                f"Telegram asks you to wait {exc.seconds} seconds before retrying.",
                code="rate_limited", retry_after_seconds=exc.seconds,
            ) from None
        except (errors.ChannelPrivateError, errors.ChannelInvalidError, errors.ChatIdInvalidError):
            raise TelegramSearchError(
                "The configured chat is unavailable to this session.", code="chat_unavailable",
            ) from None
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
        async with self._open_chat(chat_id) as peer:
            sender = await _resolve_author(self._client, author)
            return await _search_pages(self._client, peer, query, sender, since, until, limit, offset_id)

    async def search_rich(
        self,
        chat_id: int,
        *,
        query: str,
        author: int | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        offset_id: int | None,
        after_message_id: int | None = None,
    ) -> RichSearchPage:
        async with self._open_chat(chat_id) as peer:
            sender = await _resolve_author(self._client, author)
            rows, next_offset = await _search_rows(
                self._client, peer, query, sender, since, until, limit, offset_id, after_message_id,
                max_batches=1,
            )
            return RichSearchPage([_rich_message(row, peer) for row in rows], next_offset)

    async def get_search_start(self, chat_id: int, until: datetime) -> int | None:
        """Return an exclusive offset, zero for the full chat, or None for an empty range."""
        async with self._open_chat(chat_id) as peer:
            return await _search_start(self._client, peer, until)

    async def get_story_anchor(self, chat_id: int, message_id: int) -> StoryAnchor:
        async with self._open_chat(chat_id) as peer:
            message = await _get_exact_message(self._client, peer, message_id)
            if message is None:
                raise TelegramSearchError(
                    "The message is unavailable in the configured chat.", code="message_unavailable",
                )
            parent_message_id = None
            status = "none"
            reply = message.reply_to
            if isinstance(reply, types.MessageReplyHeader) and reply.reply_to_msg_id:
                if not _same_chat_reply(reply, chat_id):
                    status = "outside_chat"
                else:
                    parent_message_id = reply.reply_to_msg_id
                    status = "pending"
            return StoryAnchor(
                message=_rich_message(message, peer),
                replied_to=None,
                parent_status=status,
                is_user_author=isinstance(message.from_id, types.PeerUser),
                use_history=bool(message.post) or isinstance(peer, types.InputPeerChat),
                parent_message_id=parent_message_id,
            )

    async def get_story_parent(self, chat_id: int, message_id: int) -> RichMessage | None:
        async with self._open_chat(chat_id) as peer:
            parent = await _get_exact_message(self._client, peer, message_id)
            return _rich_message(parent, peer) if parent is not None else None

    async def get_story_replies(
        self, chat_id: int, message_id: int, *, limit: int,
        offset_id: int | None, use_history: bool = False,
    ) -> ReplyPage:
        async with self._open_chat(chat_id) as peer:
            return await _reply_page(
                self._client, peer, message_id, limit, offset_id, use_history,
            )

    async def get_context(self, chat_id: int, message_id: int, *, before: int, after: int) -> MessageContext:
        async with self._open_chat(chat_id) as peer:
            # The history offset counts the anchor itself. Move back one extra slot
            # so the window contains the anchor as well as `after` newer messages.
            rows = await _get_history(
                self._client, peer, offset_id=message_id, add_offset=-(after + 1), limit=before + after + 1,
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
                and _same_chat_reply(reply, chat_id)
            ):
                parent = messages.get(reply.reply_to_msg_id)
                if parent is None:
                    parent = await _get_exact_message(self._client, peer, reply.reply_to_msg_id)

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
        raise TelegramSearchError(
            "Author is unavailable. Try a current @username or a known sender_id.", code="author_unavailable",
        ) from None
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
    rows, next_offset = await _search_rows(
        client, peer, query, sender, since, until, limit, offset_id or None,
    )
    return SearchPage([_message_hit(row, peer) for row in rows], next_offset)


async def _search_start(
    client: TelegramClient, peer: ChatPeer, until: datetime | None,
) -> int | None:
    if until is None:
        return 0
    timestamp = ceil(until.timestamp())
    if timestamp <= 0:
        return None
    if timestamp >= 2**31:
        return 0

    # Resolve the upper date bound into an exclusive message ID cursor.
    anchor = await _get_history(client, peer, offset_date=timestamp, limit=1)
    return min(anchor[0].id + 1, 2**31 - 1) if anchor else None


async def _search_rows(
    client: TelegramClient,
    peer: ChatPeer,
    query: str | None,
    sender: types.InputPeerUser | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    offset_id: int | None,
    after_message_id: int | None = None,
    *,
    max_batches: int = 4,
) -> tuple[list[types.Message], int | None]:
    offset = await _search_start(client, peer, until) if offset_id is None else offset_id
    if offset is None:
        return [], None

    hits: list[types.Message] = []
    chat_id = utils.get_peer_id(peer)
    for _ in range(max_batches):
        result = await client(functions.messages.SearchRequest(
            peer=peer, q=query or "", from_id=sender, filter=types.InputMessagesFilterEmpty(),
            min_date=None, max_date=None, offset_id=offset, add_offset=0,
            limit=min(limit + 1, 51), max_id=0, min_id=after_message_id or 0, hash=0,
        ))
        rows = sorted(
            (row for row in result.messages if row.id > 0 and (offset == 0 or row.id < offset)),
            key=lambda row: row.id, reverse=True,
        )
        if not rows:
            return hits, None
        for row in rows:
            if after_message_id is not None and row.id <= after_message_id:
                return hits, None
            if not isinstance(row, (types.Message, types.MessageService)) or utils.get_peer_id(row.peer_id) != chat_id:
                continue
            if since is not None and row.date < since:
                return hits, None
            if not isinstance(row, types.Message) or (until is not None and row.date >= until):
                continue
            sender_id = utils.get_peer_id(row.from_id) if row.from_id is not None else None
            if sender is not None and sender_id != sender.user_id:
                continue
            # IDs advance across batches, but Telegram can repeat an ID within one batch.
            if hits and row.id == hits[-1].id:
                continue
            if len(hits) == limit:
                return hits, hits[-1].id
            hits.append(row)
        offset = rows[-1].id

    # A short page can contain unavailable messages. Keep the cursor so the
    # caller can continue after this call's fixed batch budget.
    return hits, offset


def _rich_message(message: types.Message, peer: ChatPeer) -> RichMessage:
    kind = _message_type(message)
    body = message.message if message.message and message.message.strip() else None
    has_caption = kind not in ("text", "link_preview")
    return RichMessage(
        message_id=message.id,
        date=message.date,
        sender_id=utils.get_peer_id(message.from_id) if message.from_id is not None else None,
        text=body if not has_caption else None,
        caption=body if has_caption else None,
        message_type=kind,
        text_status=("caption" if has_caption else "text") if body is not None else "absent",
        url=f"https://t.me/c/{peer.channel_id}/{message.id}" if isinstance(peer, types.InputPeerChannel) else None,
    )


def _message_type(message: types.Message) -> str:
    media = message.media
    if media is None or isinstance(media, types.MessageMediaEmpty):
        return "text"
    if isinstance(media, types.MessageMediaWebPage):
        return "link_preview"
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if isinstance(media, types.MessageMediaDocument):
        document = media.document
        if not isinstance(document, types.Document):
            return "document"
        for attribute in document.attributes:
            if isinstance(attribute, types.DocumentAttributeSticker):
                return "sticker"
            if isinstance(attribute, types.DocumentAttributeAudio):
                return "voice" if attribute.voice else "audio"
            if isinstance(attribute, types.DocumentAttributeVideo) and attribute.round_message:
                return "round_video"
        if any(isinstance(attribute, types.DocumentAttributeAnimated) for attribute in document.attributes):
            return "animation"
        if any(isinstance(attribute, types.DocumentAttributeVideo) for attribute in document.attributes):
            return "video"
        return "document"
    if isinstance(media, types.MessageMediaPoll):
        return "poll"
    if isinstance(media, types.MessageMediaContact):
        return "contact"
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        return "location"
    return "unsupported"


def _same_chat_reply(reply: types.MessageReplyHeader, chat_id: int) -> bool:
    return not reply.reply_to_scheduled and (
        reply.reply_to_peer_id is None or utils.get_peer_id(reply.reply_to_peer_id) == chat_id
    )


async def _get_exact_message(
    client: TelegramClient, peer: ChatPeer, message_id: int,
) -> types.Message | None:
    try:
        rows = await _get_history(client, peer, offset_id=message_id, add_offset=-1, limit=1)
    except (errors.MessageIdInvalidError, errors.MsgIdInvalidError):
        return None
    chat_id = utils.get_peer_id(peer)
    return next((
        row for row in rows
        if isinstance(row, types.Message) and row.id == message_id
        and row.peer_id is not None and utils.get_peer_id(row.peer_id) == chat_id
    ), None)


async def _reply_page(
    client: TelegramClient, peer: ChatPeer, message_id: int, limit: int,
    offset_id: int | None, use_history: bool,
) -> ReplyPage:
    history = use_history or isinstance(peer, types.InputPeerChat)
    offset = offset_id or 0
    chat_id = utils.get_peer_id(peer)
    if history:
        raw_rows = await _get_history(client, peer, offset_id=offset, limit=100)
    else:
        try:
            result = await client(functions.messages.GetRepliesRequest(
                peer=peer, msg_id=message_id, offset_id=offset, offset_date=None,
                add_offset=0, limit=100, max_id=0, min_id=0, hash=0,
            ))
        except errors.BadRequestError as exc:
            if not isinstance(exc, (
                errors.MsgIdInvalidError, errors.MessageIdInvalidError,
                errors.PeerIdInvalidError, errors.MethodInvalidError,
            )) and exc.message != "TOPIC_ID_INVALID":
                raise
            return ReplyPage([], offset_id, True, "more")
        raw_rows = result.messages
        # Ignore channel comments returned from a linked discussion chat. They use a different ID space.
        foreign = any(
            row.peer_id is not None and utils.get_peer_id(row.peer_id) != chat_id
            for row in raw_rows
        )
        if foreign or (not raw_rows and offset == 0):
            return ReplyPage([], offset_id, True, "more")

    rows = sorted(
        (row for row in raw_rows if row.id > 0 and (not offset or row.id < offset)),
        key=lambda row: row.id, reverse=True,
    )
    if not rows:
        if offset and any(row.id > 0 for row in raw_rows):
            raise TelegramSearchError("Telegram returned replies without advancing the cursor.", code="stalled_offset")
        return ReplyPage([], None, history, "complete")
    hits: list[RichMessage] = []
    for inspected, row in enumerate(rows, 1):
        if row.id <= message_id:
            return ReplyPage(hits, None, history, "complete", inspected)
        if not isinstance(row, types.Message) or row.peer_id is None:
            continue
        if utils.get_peer_id(row.peer_id) != chat_id:
            continue
        reply = row.reply_to
        if not isinstance(reply, types.MessageReplyHeader) or (
            reply.reply_to_msg_id != message_id or not _same_chat_reply(reply, chat_id)
        ):
            continue
        if hits and hits[-1].message_id == row.id:
            continue
        if len(hits) == limit:
            return ReplyPage(hits, hits[-1].message_id, history, "more", inspected)
        hits.append(_rich_message(row, peer))
    return ReplyPage(hits, rows[-1].id, history, "more", len(rows))
