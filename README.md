# Telegram File Downloader Bot

**Made By @Yorichiiprime**

A lightweight Telegram bot that downloads files from share links and delivers
them in your chat. **Just paste a link** — GoFile, Google Drive, Dropbox,
OneDrive, MediaFire, Pixeldrain, Internet Archive, or any direct file URL —
tap **「 DOWNLOAD 」**, and the bot streams the file and sends it to you
through the Telegram Bot API.

Built with **Python 3.11+**, **python-telegram-bot** (async) and **aiohttp** —
no database, no Redis, no persistent user storage, and **no heavy media
toolchain** (no yt-dlp, no ffmpeg). Every downloader resolves a link from
small JSON/HTML metadata and streams the file to disk in 64 KB chunks, so the
bot runs comfortably on a **512 MB** instance (~50 MB at idle; downloads are
globally capped at 3 concurrent).

---

## Supported sources

| Source | Example | How it downloads | Notes |
| --- | --- | --- | --- |
| **GoFile** | `gofile.io/d/xxxxxx` | GoFile API (guest token or your `GOFILE_API_TOKEN`) | Files & folders; `/gofile <url> <password>` for protected shares |
| **Google Drive** | `drive.google.com/file/d/…/view` | Public `uc?export=download` flow (incl. large-file confirm form) | Docs → PDF, Sheets → XLSX, Slides → PPTX. No folders |
| **Dropbox** | `dropbox.com/scl/fi/…?dl=0` | `dl=1` force-download form | Shared folders arrive as Dropbox's own `.zip` |
| **OneDrive / SharePoint** | `1drv.ms/…`, `*.sharepoint.com/:b:/…` | Graph shares endpoint / classic `download?cid=…` form | "Anyone with the link" only. No folders |
| **Pixeldrain** | `pixeldrain.com/u/…`, `/l/…` | Official public API | Files and lists |
| **MediaFire** | `mediafire.com/file/…/file` | Reads the public download button on the file page | Single files (no folders) |
| **Internet Archive** | `archive.org/details/…` | Public metadata API | Item pages resolve to their file list |
| **Direct link** | `example.com/file.zip` | HEAD probe + stream, SSRF-guarded | Any public http(s) URL that points straight at a file |

Only **public** links work anywhere: content that needs a login, a paid
account, or a CAPTCHA stays inaccessible (the bot never asks users for
credentials and never bypasses access controls).

### Deliberately not included (512 MB budget)

- **YouTube / Instagram / TikTok / X via yt-dlp + ffmpeg** — format merging and
  MP3 conversion spike to hundreds of MB. Revisit only on a bigger server.
- **Torrents / magnets** — a torrent engine alone exceeds the RAM budget.
- **MEGA.nz** — feasible RAM-wise, but pure-Python decryption is CPU-heavy;
  possible later behind a 1-at-a-time limit.
- **Terabox** — needs a personal account cookie that expires; possible later
  if the operator wants to maintain one.

## Features

- **Paste any supported link** (no command needed) or use `/download <url>`.
- Shows **name, size, type and source** with an inline **「 DOWNLOAD 」** button.
- On demand: streams the file to a temporary directory (live progress in the
  same status message) and sends it as a Telegram document.
- **Folder/list shares** (GoFile, Pixeldrain lists, archive.org items) deliver
  up to 10 sendable files per request, each as its own document.
- Password-protected GoFile shares: `/gofile <url> <password>`.
- **Cancel** button, one running download per user, max **3 concurrent
  downloads globally** (extras wait in a visible queue), so a 512 MB box
  stays healthy under load.
- Graceful handling of invalid links, dead/private content, wrong passwords,
  rate limits (bounded backoff), timeouts, oversized files, Telegram's upload
  limit, and user cancellation.
- **SSRF protection**: known hosts are pinned to per-provider host allowlists;
  arbitrary direct links must resolve exclusively to public IPs, and every
  redirect hop is re-validated.
- Temporary files are always cleaned up after delivery or failure.

## Commands

| Command | Description |
| --- | --- |
| `/start` | Welcome message |
| `/help` | Usage and limits |
| `/download <url>` | Download from any supported link (alias `/dl`) |
| `/gofile <url>` | Fetch a GoFile share |
| `/gofile <url> <password>` | Password-protected GoFile share (password may contain spaces) |
| `/supported` | List every supported source (alias `/sites`) |
| `/ping` | Check the bot is alive |

Pasting a link works for all supported hosts, and for direct links whose URL
ends in a file extension (`.zip`, `.mp4`, …). Use `/download` to force any
other direct URL.

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
# ENABLE_DIRECT_DOWNLOADS=true
```

| Variable | Required | Meaning |
| --- | --- | --- |
| `BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `GOFILE_API_TOKEN` | no | Your own GoFile account token (from [gofile.io/myprofile](https://gofile.io/myprofile)). If set, the bot uses it instead of creating a throwaway guest account. A **Premium** token lifts GoFile's guest rate limits. |
| `GOFILE_WT_SALT` | no | Salt used to derive GoFile's rotating website token. The default matches GoFile's public web client. Only change it if GoFile rotates it. |
| `ENABLE_DIRECT_DOWNLOADS` | no | `false` disables arbitrary direct http(s) file links (known file hosts keep working). Default `true`. |
| `PORT` | no | Port for the `/health` server (default `10000`); Render sets this automatically. |

Nothing else is read from `.env`. Operational limits (size caps, timeouts,
concurrency) are constants in [`gofile_bot/config.py`](gofile_bot/config.py).
No other downloader needs an API key.

### 4. Run

```bash
python -m gofile_bot
```

Useful flags:

```bash
python -m gofile_bot --debug            # verbose logging
python -m gofile_bot --env-file /path/to/.env
```

The bot runs long polling — no webhook or public IP needed. At the same time
it serves `GET /health` → `200 OK` on `0.0.0.0:$PORT` (default `10000`) for
Docker and Render health checks. Stop with `Ctrl+C`.

---

## Deploying to Render (or Docker)

Because the bot uses Telegram **long polling** and also serves `GET /health`
on `0.0.0.0:$PORT`, it runs cleanly as a Render **Web Service** using the
included [`Dockerfile`](Dockerfile) and [`render.yaml`](render.yaml). A
512 MB instance is enough.

### Option A — One-click Render Blueprint

1. Push this repo to GitHub.
2. In Render: **New → Blueprint** → select the repo (it reads `render.yaml`).
3. Paste your `BOT_TOKEN` when prompted (and optionally `GOFILE_API_TOKEN`).
4. Deploy. Render injects `PORT`, builds the Dockerfile, and probes `/health`.

### Option B — Manual Render Web Service

1. **New → Web Service** → connect your repo.
2. **Runtime:** `Docker`
3. **Health Check Path:** `/health`
4. **Environment Variables:** add `BOT_TOKEN` (and optional `GOFILE_API_TOKEN`).

> **About Render plans:** Render's **Free** web tier spins down after ~15 min
> of no inbound HTTP traffic — while spun down, long polling stops until
> something hits `/health` (or you upgrade to the $7/mo **Starter** plan /
> Background Worker, which never sleeps). If you stay on Free, you can point a
> free uptime monitor (e.g. UptimeRobot, every 5–10 min) at
> `https://<your-service>.onrender.com/health` to keep it awake, within
> Render's 750 free instance-hours/month.
>
> **Alternatives that work with the exact same `Dockerfile`:** Fly.io,
> Railway, Koyeb, Northflank, or any small VPS (`docker run`).

### Plain Docker

```bash
docker build -t gofile-bot .
docker run -d --name gofile-bot --restart unless-stopped \
  -p 10000:10000 \
  --memory=512m \
  --env-file .env \
  gofile-bot

curl http://localhost:10000/health   # -> OK
```

---

## Usage

In any chat with the bot, paste a link:

```
https://drive.google.com/file/d/1AbC_dEf-GhIjK/view
```

or use a command:

```
/download https://pixeldrain.com/u/abc12345
/gofile https://gofile.io/d/hXHGR1 TestPassword
```

1. The bot immediately replies `⌕ Fetching file information from …`
2. That message is edited to show the file name, size, type and a
   **「 DOWNLOAD 」** button.
3. Tap the button — the bot downloads the file, updates progress in the same
   message, and sends it as a document.

---

## How the bot accesses files (and stays safe)

- **GoFile:** share links are parsed strictly (`gofile.io/d/<code>`); the
  extracted content id is the only thing that reaches the GoFile API. For
  public shares the bot reproduces exactly what GoFile's own web client does
  for an anonymous visitor (free *guest* account via the documented
  `POST /accounts`, plus GoFile's rotating client-side website token).
  **No authentication is bypassed**: private content stays inaccessible, and
  password-protected content still requires the password. If you provide
  `GOFILE_API_TOKEN` it is used as-is (documented bearer auth).
- **Other hosts:** only their public, logged-out flows are used — official
  APIs where they exist (Pixeldrain, archive.org), the public share/download
  endpoints (Google Drive, Dropbox, OneDrive), or the public download button
  (MediaFire).
- **SSRF:** every provider pins downloads to its own host allowlist and
  re-validates each redirect hop. The direct-link provider additionally
  requires the host to resolve *only* to globally-routable IPs (private,
  loopback, link-local and reserved ranges are refused, as are URLs with
  embedded credentials). Residual DNS-rebinding risk is the standard one for
  URL fetchers; set `ENABLE_DIRECT_DOWNLOADS=false` if you don't need it.
- **Rate limits are respected**: bounded exponential backoff on GoFile
  `error-rateLimit`, one active download per user, and a global cap of
  3 concurrent transfers.

Please only download content you have the right to access, and respect each
service's terms.

## Limits

| Limit | Value | Where to change |
| --- | --- | --- |
| Max file size | 48 MB (Telegram Bot API upload limit is 50 MB) | `MAX_FILE_BYTES` |
| Max files per folder/list | 10 | `MAX_FOLDER_FILES` |
| Max total per folder/list | 300 MB | `MAX_FOLDER_TOTAL_BYTES` |
| Concurrent downloads (all users) | 3 | `MAX_CONCURRENT_DOWNLOADS` |
| Max job duration | 30 min | `JOB_TIMEOUT` |

Running a [Local Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
raises Telegram's limits to 2 GB — raise `MAX_FILE_BYTES` accordingly.

---

## Project structure

```
.
├── gofile_bot/
│   ├── __main__.py            # CLI entry point: python -m gofile_bot
│   ├── app.py                 # Application assembly + lifecycle (bot + /health)
│   ├── config.py              # .env settings + operational constants
│   ├── errors.py              # Typed errors with friendly user messages
│   ├── health.py              # Tiny aiohttp server: GET /health -> 200 OK
│   ├── downloaders/           # One provider module per source
│   │   ├── __init__.py        # build_registry(): routing order
│   │   ├── base.py            # Downloader interface + registry
│   │   ├── net.py             # SSRF guard, redirect-safe streaming & probing
│   │   ├── gofile_provider.py # Adapter over the GoFile client below
│   │   ├── gdrive.py          # Google Drive (+ Docs/Sheets/Slides export)
│   │   ├── dropbox.py         # Dropbox shared links
│   │   ├── onedrive.py        # OneDrive / SharePoint shared links
│   │   ├── pixeldrain.py      # Pixeldrain (official API)
│   │   ├── mediafire.py       # MediaFire public file pages
│   │   ├── archive_org.py     # Internet Archive (metadata API)
│   │   └── direct.py          # Arbitrary direct http(s) file links
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
│       ├── handlers.py        # Commands, pasted links, button callbacks
│       └── keyboards.py       # Inline keyboards
├── tests/                     # Unit tests (mocked network, no real API calls)
├── Dockerfile                 # Non-root python:3.11-slim image with HEALTHCHECK
├── render.yaml                # Render Blueprint (Docker web service + /health)
├── requirements.txt
├── requirements-dev.txt
├── .env.example
└── pytest.ini
```

**Adding another source later** = one new module in `downloaders/`
implementing `matches` / `resolve` / `download`, plus one line in
`build_registry`. Handlers, jobs and transfers don't change.

## Development

```bash
pip install -r requirements-dev.txt
pytest                        # run the test suite
ruff check gofile_bot tests   # lint (optional)
```

The tests cover URL/SSRF validation, website-token derivation, payload
parsing, every downloader's link parsing/routing, redirect safety, the GoFile
client (rate-limit backoff, token-window retry, guest token refresh,
streaming downloads, cancellation, size caps), job management, the global
concurrency cap, transfer orchestration and the Telegram handlers — all
against mocked transports. No live Telegram or file-host calls are made.

> **Note for test environments:** `aioresponses` (the HTTP mock) currently
> supports aiohttp < 3.14, so `requirements-dev.txt` caps aiohttp for tests.
> Runtime (`requirements.txt`) works with any aiohttp 3.x.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `error: BOT_TOKEN is not set` | Copy `.env.example` to `.env` and set `BOT_TOKEN`. |
| "I don't know how to download from that link" | Check `/supported`; for direct URLs use `/download <url>`. |
| "That link opens a web page, not a direct file" | The URL isn't a direct file. Use a supported host link instead. |
| "private or needs a login" | Share the file as "Anyone with the link", then retry. |
| Drive/OneDrive *folder* rejected | Folders aren't supported there — link a single file. |
| "GoFile is rate-limiting requests" | Wait a few minutes; heavy users can set `GOFILE_API_TOKEN`. |
| "too big for Telegram uploads" | Telegram's Bot API caps uploads at 50 MB (see Limits). |
| "This button has expired" | Jobs live in memory for 24 h (and across restarts they reset) — send the link again. |
| "Queued — waiting for a free download slot…" | 3 downloads are already running; yours starts automatically. Raise `MAX_CONCURRENT_DOWNLOADS` on bigger instances. |
| GoFile downloads suddenly fail with "not accepting requests" | GoFile may have rotated the salt in its web client; update `GOFILE_WT_SALT` with the current value from `gofile.io`'s `wt` script, or set a Premium `GOFILE_API_TOKEN`. |
| MediaFire/Kraken-style host stops working | Page-scrape providers break when the site changes its HTML — open an issue with a sample link. |

## Credits

Made By @Yorichiiprime

## License

See [LICENSE](LICENSE).
