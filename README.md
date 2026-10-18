# GoFile.io Telegram File Downloader Bot

A Telegram bot that downloads files from [GoFile.io](https://gofile.io) share
links and delivers them in your chat.

Send `/gofile https://gofile.io/d/xxxxxx`, tap **「 DOWNLOAD 」**, and the bot
streams the file from GoFile and sends it to you through the Telegram Bot API.

Built with **Python 3.11+**, **python-telegram-bot** (async) and **aiohttp** —
no database, no Redis, no persistent user storage.

---

## Features

- `/gofile <url>` fetches file metadata and shows **name, size and type** with
  an inline **「 DOWNLOAD 」** button.
- On demand: streams the file to a temporary directory (with progress updates
  in the same status message) and sends it as a Telegram document.
- Single **files** and **folders** (albums) — folders deliver up to 10 sendable
  files per request, each as its own document.
- Password-protected shares: `/gofile <url> <password>`.
- Live progress (throttled edits), **Cancel** button, one download per user,
  concurrent users supported.
- Graceful handling of: invalid links, dead/removed content, missing/wrong
  passwords, GoFile rate limits (bounded backoff), timeouts, oversized files,
  Telegram's upload limit, and user cancellation.
- Strict URL validation — **only `gofile.io` share links are accepted**;
  download requests are re-validated against a `*.gofile.io` host allowlist
  (no SSRF).
- Temporary files are always cleaned up after delivery or failure.

## Commands

| Command | Description |
| --- | --- |
| `/start` | Welcome message |
| `/help` | Usage and limits |
| `/gofile <url>` | Fetch a GoFile share |
| `/gofile <url> <password>` | Password-protected share (password may contain spaces) |

---

## Setup

### 1. Create a bot

Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and
copy the **bot token**.

### 2. Install

Requires **Python 3.11+**.

```bash
git clone https://github.com/harshi79/GO-FILE-DOWNLODER-API.git
cd GO-FILE-DOWNLODER-API

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```ini
BOT_TOKEN=123456789:your-token-from-botfather

# Optional — see below
GOFILE_API_TOKEN=
GOFILE_WT_SALT=12af056dacea0b
```

| Variable | Required | Meaning |
| --- | --- | --- |
| `BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `GOFILE_API_TOKEN` | no | Your own GoFile account token (from [gofile.io/myprofile](https://gofile.io/myprofile)). If set, the bot uses it instead of creating a throwaway guest account. A **Premium** token lifts GoFile's guest rate limits. |
| `GOFILE_WT_SALT` | no | Salt used to derive GoFile's rotating website token. The default matches GoFile's public web client. Only change it if GoFile rotates it. |

Nothing else is read from `.env`. Operational limits (size caps, timeouts)
are constants in [`gofile_bot/config.py`](gofile_bot/config.py).

### 4. Run

```bash
python -m gofile_bot
```

Useful flags:

```bash
python -m gofile_bot --debug            # verbose logging
python -m gofile_bot --env-file /path/to/.env
```

The bot runs long polling — no webhook or public IP needed. Stop with
`Ctrl+C`.

---

## Usage

In any chat with the bot:

```
/gofile https://gofile.io/d/AbCdEf12
```

1. The bot immediately replies `⌕ Fetching file information...`
2. That message is edited to show the file name, size, type and a
   **「 DOWNLOAD 」** button.
3. Tap the button — the bot downloads the file, updates progress in the same
   message, and sends it as a document.

For a password-protected folder:

```
/gofile https://gofile.io/d/hXHGR1 TestPassword
```

---

## How the bot accesses GoFile

- Share links are parsed strictly (`gofile.io/d/<code>`); the extracted
  content id is the only thing that ever reaches the GoFile API.
- GoFile's documented read endpoint (`GET /contents/{id}`) is Premium-only for
  direct API access. For public shares the bot reproduces exactly what GoFile's
  **own web client** does for an anonymous visitor: it creates a free *guest*
  account (the documented public entry point, `POST /accounts`) and signs
  requests with GoFile's rotating client-side website token. This is ordinary
  public-share access — **no authentication is bypassed**: private content
  stays inaccessible, and password-protected content still requires the
  password.
- Rate limits are respected: bounded exponential backoff on `error-rateLimit`,
  a global cap on concurrent GoFile requests, and one active download per user.
- If you provide `GOFILE_API_TOKEN`, it is used as-is (the documented bearer
  authentication) — recommended for heavy use.

## Limits

| Limit | Value | Where to change |
| --- | --- | --- |
| Max file size | 48 MB (Telegram Bot API upload limit is 50 MB) | `MAX_FILE_BYTES` |
| Max files per folder | 10 | `MAX_FOLDER_FILES` |
| Max total per folder | 300 MB | `MAX_FOLDER_TOTAL_BYTES` |
| Max job duration | 30 min | `JOB_TIMEOUT` |

Running a [Local Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
raises Telegram's limits to 2 GB — raise `MAX_FILE_BYTES` accordingly.

---

## Project structure

```
.
├── gofile_bot/
│   ├── __main__.py            # CLI entry point: python -m gofile_bot
│   ├── app.py                 # Application assembly, lifecycle hooks
│   ├── config.py              # .env settings + operational constants
│   ├── errors.py              # Typed errors with friendly user messages
│   ├── gofile/
│   │   ├── client.py          # Async GoFile client (metadata + streaming DL)
│   │   ├── models.py          # Content/file/folder models, payload parsing
│   │   ├── token.py           # GoFile's rotating website-token derivation
│   │   └── urls.py            # Strict share-URL parsing, SSRF allowlist
│   ├── services/
│   │   ├── jobs.py            # In-memory job registry (one job per request)
│   │   ├── status.py          # Throttled edits of the status message
│   │   └── transfer.py        # Download→upload orchestration + cleanup
│   └── telegram/
│       ├── formatting.py      # HTML message builders
│       ├── handlers.py        # /start /help /gofile + button callbacks
│       └── keyboards.py       # Inline keyboards
├── tests/                     # Unit tests (mocked network, no real API calls)
├── requirements.txt
├── requirements-dev.txt
├── .env.example
└── pytest.ini
```

## Development

```bash
pip install -r requirements-dev.txt
pytest                        # run the test suite
ruff check gofile_bot tests   # lint (optional)
```

The tests cover URL/SSRF validation, website-token derivation, payload
parsing, the GoFile client (rate-limit backoff, token-window retry, guest
token refresh, streaming downloads, cancellation, size caps), job management,
transfer orchestration and the Telegram handlers — all against mocked
transports. No live Telegram or GoFile calls are made.

> **Note for test environments:** `aioresponses` (the HTTP mock) currently
> supports aiohttp &lt; 3.14, so `requirements-dev.txt` caps aiohttp for tests.
> Runtime (`requirements.txt`) works with any aiohttp 3.x.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `error: BOT_TOKEN is not set` | Copy `.env.example` to `.env` and set `BOT_TOKEN`. |
| "GoFile is rate-limiting requests" | Wait a few minutes; heavy users can set `GOFILE_API_TOKEN`. |
| "too big for Telegram uploads" | Telegram's Bot API caps uploads at 50 MB (see Limits). |
| "This button has expired" | Jobs live in memory for 24 h (and across restarts they reset) — send `/gofile` again. |
| Downloads suddenly fail with "not accepting requests" | GoFile may have rotated the salt in its web client; update `GOFILE_WT_SALT` with the current value from `gofile.io`'s `wt` script, or set a Premium `GOFILE_API_TOKEN`. |

## License

See [LICENSE](LICENSE).
