# tg-mcp

`tg-mcp` is a local MCP server that lets Codex search Telegram chats. Configure the chat list in `config.toml`. The server searches by keyword and author through the Telegram API and returns the message text, date, author ID, and a link when available.

## Installation

You need Python 3.11 or later, a Telegram account, and an `api_id` and `api_hash`. Run the commands from the project directory. [Telegram API development tools](https://my.telegram.org/apps)

Windows (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Linux and macOS

```sh
python3 -m venv .venv
. .venv/bin/activate
```

Activate the environment and install the project with its pinned dependencies.

```console
python -m pip install -r requirements.txt .
```

For the initial setup, copy `config.example.toml` to `config.toml` and fill in `api_id` and `api_hash` under `[telegram]`. The `session` path is relative to the directory containing the configuration file. The `login` command prompts for a login code and, if you enabled two-step verification, your password.

By default, `tg-mcp` and `tg-mcp-setup` read `config.toml` from the current directory. Use `--config` to select a different file, as in `tg-mcp-setup --config config.local.toml chats`.

```console
tg-mcp-setup login
tg-mcp-setup chats --toml
```

The `chats` command caches chat IDs in the session. With `--toml`, it prints a ready-to-paste `[chats]` section, adds chat titles as comments, and preserves aliases already in your configuration. Keep the entries you want and replace the section in `config.toml`. To add chats to an existing list, paste their entries under the existing `[chats]` heading.

```toml
[chats]
community = -1001234567890
housing = -1009876543210
```

`config.toml` and the `.session` file contain credentials. Git ignores both when you use the example filenames. The setup commands and the MCP server share the same Telegram session and lock it while in use. If the session is busy, stop the process using it and retry.

## Run the server and connect Codex

One `tg-mcp` process holds the Telegram session and serves requests from multiple Codex chats concurrently over Streamable HTTP. All clients share the same list of allowed Telegram chats.

1. Add an `[http]` section to the project's `config.toml`. Generate a `token` with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

   ```toml
   [http]
   port = 8765
   token = "replace-with-your-generated-token"
   ```

2. Stop any existing `tg-mcp` process that uses the same Telegram session. Start the server from the project directory and leave it running. It listens only on `127.0.0.1` and requires the token on every request.

   ```console
   tg-mcp
   ```

3. Add the `[mcp_servers.telegram]` section to your Codex configuration or replace the existing section. Use the same token and port as in the project's configuration, then launch Codex. [Codex MCP settings](https://learn.chatgpt.com/docs/config-file/config-reference#mcp-servers)

   ```toml
   [mcp_servers.telegram]
   url = "http://127.0.0.1:8765/mcp"
   http_headers = { Authorization = "Bearer replace-with-your-generated-token" }
   startup_timeout_sec = 25
   ```

Restart the server to load changes to the Telegram chat list. Before running `tg-mcp-setup`, stop the server with Ctrl+C to release the session.

For diagnostics, start the server with `tg-mcp --diagnostics`. Each tool call gets an ID in stderr. Its log entries show the Telegram method, elapsed milliseconds, reported wait, and whether the request was dispatched to Telethon or blocked by a cooldown. The tool summary counts dispatches to Telethon. Created and consumed cursors have matching fingerprints. Logs omit message text, search phrases and usable cursors.

All clients share four request slots for one Telegram session. When Telegram returns a flood wait, new calls to the affected method receive the remaining delay locally until the wait expires. Calls already in flight can still receive a flood wait of their own.

## Search

- `list_search_chats()` returns only the aliases under `[chats]`.
- `search_messages(chat, query?, author?, since?, until?, limit?, offset_id?)` searches one chat. Provide `query`, `author`, or both. The `author` filter accepts a numeric Telegram user ID or an exact `@username`. The `since` bound is inclusive and `until` is exclusive. Use timestamps with time zones. Page sizes range from 1 to 50.
- `get_message_context(chat, message_id, before=5, after=5)` returns the message, nearby messages, and the same-chat message it replied to, even if that message lies outside the window. Take the chat alias and ID from the search result. You can request between 0 and 20 history entries on either side. Service messages and unavailable entries may shorten the result. In forums, the window follows the entire chat's timeline.
- To fetch the next page from `search_messages`, pass `next_offset_id` as `offset_id` and keep the other filters fixed. Continue until `next_offset_id` is `null`, even if an intermediate page is empty.

Example prompts for Codex

> Find messages about deposits in `community` and `housing` from the last three months. Include the date, chat, and link for each case.

> Build a timeline of messages from `@example` in `community` from January 1 to April 1, Moscow time. Read every page in that range. Include message links and the dates covered by the search.

For searches over longer periods, use the numeric `sender_id` from your results. It stays the same when a user changes their `@username`. Telethon can usually resolve a numeric ID only if the session has already encountered that user. If the ID is unavailable, try their current `@username`. Searches cover the history your session can access in the selected chats. [How Telethon resolves IDs](https://docs.telethon.dev/en/stable/concepts/entities.html#entities-vs-input-entities)

### Search multiple chats and phrases

`search_messages_multi` searches for any of the supplied phrases and returns unique messages, newest first. The limit applies to the whole page. You can specify up to eight chats, up to eight phrases, and up to 32 chat–phrase pairs. Get aliases from `list_search_chats`. The first call fixes the upper date bound.

The page limit for search, direct replies and follow-up search is 1–50. The default is 20.

The following example covers the 90 days before November 19, 2026.

```json
{
  "chats": ["chat_a", "chat_b", "chat_c"],
  "queries": ["language course visa", "got an appointment", "request for more documents", "rescheduled my course"],
  "since": "2026-08-21T00:00:00+03:00",
  "until": "2026-11-19T00:00:00+03:00",
  "limit": 20,
  "max_text_chars": 300
}
```

To search for one person across the selected chats, pass their numeric `sender_id` from an earlier result as `author`.

For a first pass, use a small `limit` and set `max_text_chars` to cap the text or caption in each hit. A value of `0` returns only metadata. When content is shortened, `content_truncated` is `true`. The tool still checks the full message to determine the match source. Omit the cap to return all available text. To read a full message from a shortened result, pass its `chat` and `message_id` to `get_message_story`.

For each hit, `matched_queries` lists the searches that returned it. `match_sources` marks each phrase as literally present in the text or caption, or as `unknown`. The `message_type`, `text`, `caption`, and `text_status` fields describe the content available to the server. For media without visible text or a caption, `text_status` is `absent`. The `url` field contains a message link when the server can build one. [Telegram search](https://core.telegram.org/method/messages.search) · [Telegram message fields](https://core.telegram.org/constructor/message)

To continue, pass `{"cursor":"<next_cursor>"}` to the same tool. Keep going while a cursor is present, even after a short or empty page. Cursors are valid for up to 30 minutes. They may be evicted under heavy use and expire when the server restarts.

When `partial` is `true`, `errors` identifies the chat–phrase pairs that failed. If a page provides a `retry_cursor`, save it. When Telegram specifies a `retry_after_seconds` delay, wait that long before retrying. The retry results have their own ordering. When merging both lists, deduplicate by `(chat, message_id)` and combine all `matched_queries`.

Track failed streams by `stream_id` across both branches. An error with `inherited: true` records a failure from an earlier page on that branch. Use new errors to schedule retries. A recovery page lists a stream in `completed_stream_ids` when it has exhausted that stream. Keep that completion even if another branch subsequently reports the inherited error. Follow both branches until their cursors run out.

A client can merge pages from both branches in any order.

```python
matches, failed, recovered = {}, set(), set()
for page in pages:
    for hit in page["hits"]:
        key = (hit["chat"], hit["message_id"])
        matches.setdefault(key, {}).update(hit["match_sources"])
    failed.update(issue["stream_id"] for issue in page["errors"])
    recovered.update(page["completed_stream_ids"])
outstanding = failed - recovered
```

### Message story

`get_message_story` returns the original message, the message it replied to when available, and its direct `replies` in the configured chat. The optional `follow_up` searches in the same chat for later messages by the same author that match the supplied phrases. Those matches may refer to a different application.

The following example opens result 12345 and looks for follow-up messages.

```json
{
  "chat": "chat_a",
  "message_id": 12345,
  "replies_limit": 5,
  "follow_up": {
    "queries": ["got my visa", "request for more documents", "visa refusal"],
    "until": "2026-11-19T00:00:00+03:00",
    "limit": 10,
    "max_text_chars": 300
  }
}
```

`replies.status` is `more` while another page is available, even if the current page is empty. On error, `partial` means the page contains some replies, while `failed` means the page stopped before it could return any. Pass `replies.next_cursor` and `replies.retry_cursor` to `get_message_story`. If Telegram cannot return the reply thread, the tool scans the chat history in batches. [Telegram reply threads](https://core.telegram.org/method/messages.getReplies)

Save the original message, the parent message, and the `follow_up` results from the first response. On subsequent reply pages, `context_text_included` is `false`. Those pages keep the original and parent message metadata and return the full text of new replies. Pass cursors from `follow_up.result` to `search_messages_multi`. If the parent lookup fails, `parent_status` is `failed` and `parent_error` gives the reason. The original message is still available.

`replies.scan_mode` identifies a reply-thread lookup (`reply_thread`) or a scan of local chat history (`chat_history`). `rows_scanned` counts row inspections along this cursor chain, including pages that found no direct replies. `stop_reason` explains why a page ended, for example `batch_budget`, `time_budget`, `limit`, or `rate_limited`. `resume_before_id` is the exclusive position for the next scan when one has been established. Telegram does not supply a reliable count of the remaining inspections. Use the opaque cursor to continue.

For whole-call errors, `search_messages_multi` and `get_message_story` return MCP `isError` with `code`, `message`, `retry_after_seconds` and `retry_cursor` in the structured result and in the JSON text fallback. A temporary failure reading the original message includes a cursor for the original request and its filters. Pass it back as `cursor` after the delay. For errors within a successful page, read the same error details from a search entry in `errors`, from `parent_error`, or from `replies.issue`. The reply cursor stays under `replies`.

**Coverage.** Results depend on Telegram search and the history the current session can access. Edits and deletions between pages can change the results. Telegram enforces its own access controls when opening message links. The match source may be unknown for messages without visible text.

## Tests

For development, install the package in editable mode in the active environment. The tests then use the current code under `src/`.

```console
python -m pip install -e .
python -m unittest discover -s tests -v
```

The tests use mocked Telegram responses. They cover the chat allowlist, compatibility with the original tools, pagination, retries, replies, and media without text.

## License

[MIT](LICENSE)
