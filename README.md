# Поиск по чатам Telegram из Codex

Локальный MCP-сервер ищет по ключевым словам, по автору или по обоим фильтрам. Список доступных групп и каналов задаётся в `config.toml`. Поиск выполняется в Telegram и возвращает текст, дату, идентификатор автора и ссылку на сообщение, если Telegram поддерживает ссылку для этого чата.

## Подготовка на Windows

Понадобятся Python 3.11 или новее, аккаунт Telegram и `api_id` с `api_hash` из [Telegram API development tools](https://my.telegram.org/apps). Для новой установки в PowerShell из папки проекта выполните:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
Copy-Item config.example.toml config.toml
```

Для воспроизводимой установки на Windows x64 с Python 3.12 возьмите `requirements-win-py312.lock`. Вместо `pip install .` выполните:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-win-py312.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation .
```

Укажите `api_id` и `api_hash` в секции `[telegram]`. Затем войдите в Telegram и получите идентификаторы нужных групп и каналов:

```powershell
.\.venv\Scripts\tg-mcp-setup.exe --config config.toml login
.\.venv\Scripts\tg-mcp-setup.exe --config config.toml chats
```

Команда `chats` выводит список в вашем терминале и заполняет кеш идентификаторов этой сессии. Скопируйте нужные ID в секцию `[chats]` и задайте короткие имена. Сохраните знак `-` и префикс `-100`, если он есть.

```toml
[chats]
relocation = -1001234567890
housing = -1009876543210
```

`config.toml` и файл `.session` содержат данные доступа. Храните их локально. Для настройки и сервера используется один файл сессии. Перед повторным запуском `chats` закройте процесс MCP, который держит этот файл.

## Подключение к Codex

В **Settings → MCP servers → Add server** выберите **STDIO** и укажите команду `C:\dev\tg-mcp\.venv\Scripts\tg-mcp.exe` с аргументами `--config` и `C:\dev\tg-mcp\config.toml`. Сохраните и перезапустите Codex. Та же настройка в `config.toml` Codex выглядит так:

```toml
[mcp_servers.telegram_relocation]
command = 'C:\dev\tg-mcp\.venv\Scripts\tg-mcp.exe'
args = ['--config', 'C:\dev\tg-mcp\config.toml']
startup_timeout_sec = 25
```

## Поиск

- `list_search_chats()` возвращает только имена из `[chats]`.
- `search_messages(chat, query?, author?, since?, until?, limit?, offset_id?)` ищет в одном чате. Укажите `query`, `author` или оба параметра. `author` принимает числовой Telegram ID пользователя или точный `@username`. `since` включается в диапазон, `until` исключается. Используйте дату и время с часовым поясом. Размер страницы от 1 до 50.
- `get_message_context(chat, message_id, before=5, after=5)` возвращает найденную реплику, ближайшие сообщения до и после неё и исходное сообщение, если реплика была ответом внутри этого чата. Возьмите имя чата и ID из результата поиска. Размер окна с каждой стороны от 0 до 20. В чатах с темами окно показывает общую хронологию чата.
- Для поиска по нескольким чатам Codex вызывает инструмент для каждого выбранного имени. Для следующей страницы передайте `next_offset_id` как `offset_id` и сохраните остальные ограничения. Продолжайте до `next_offset_id: null`, даже если промежуточная страница пуста. История идёт от новых сообщений к старым, при выводе хронологии её можно отсортировать по датам.

Примеры запросов к Codex:

> В чатах `relocation` и `housing` найди сообщения про депозит за последние три месяца. Укажи дату, чат и ссылку для каждого случая.

> Собери хронологию сообщений пользователя `@example` в чате `relocation` с 1 января по 1 апреля по московскому времени. Пройди все страницы за этот период. Укажи ссылки на сообщения и даты, которые покрывает поиск.

Для длинных историй удобнее указывать числовой `sender_id`, который возвращается в результатах. Он сохраняется при смене `@username`. Поиск покрывает сообщения, доступные вашей сессии в выбранных чатах. Сообщение от имени канала относится к этому каналу, даже если его написал участник группы.

## Проверка

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Тесты используют подставные ответы Telegram и проверяют границу разрешённых чатов, поиск с продолжением выдачи и чтение контекста.
