# Telegram File Downloader Bot

**Made By @Yorichiiprime**

A lightweight Telegram bot that downloads files from share links and delivers
them in your chat. **Just paste a link** — GoFile, Google Drive, Dropbox,
OneDrive, MediaFire, Pixeldrain, Internet Archive, or any direct file URL —
tap **「 DOWNLOAD 」**, and the bot streams the file and sends it to you
through the Telegram Bot API, or optional MTProto for files up to ~2 GB.

Built with **Python 3.11+**, **python-telegram-bot** (async) and **aiohttp** —
no database, no Redis, no persistent user storage, and **no heavy media
toolchain** (no yt-dlp, no ffmpeg). Optional MTProto uploads use Telethon + cryptg.
Every downloader resolves a link from
small JSON/HTML metadata and streams the file to disk in 64 KB chunks. The bot is designed for a **512 MB** instance with **one active transfer**,
a bounded queue, and isolated ZIP extraction. See the measured offline
resource smoke test below; this is not a promise of unlimited throughput.

---

## Supported sources

| Source | Example | How it downloads | Notes |
| --- | --- | --- | --- |
| **GoFile** | `gofile.io/d/xxxxxx` | GoFile API (guest token or your `GOFILE_API_TOKEN`) | Files & folders; `/gofile <url> <password>` for protected shares |
| **Google Drive** | `drive.google.com/file/d/…/view` | `drive.usercontent.google.com/download?…&confirm=t` (large-file confirm form handled) | Docs → PDF, Sheets → XLSX, Slides → PPTX. No folders |
| **Dropbox** | `dropbox.com/scl/fi/…?dl=0` | `dl=1` force-download form | Shared folders arrive as Dropbox's own `.zip` |
| **OneDrive / SharePoint** | `1drv.ms/…`, `*.sharepoint.com/:b:/…` | Anonymous OneDrive shares API (the same flow the web client uses) / `?download=1` | "Anyone with the link" only. No folders |
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
- Files that exceed the active Telegram upload limit are flagged on that
  card (no dead Download button) with a `/links` hint, instead of failing
  only after the whole download has run.
- On demand: streams the file to a temporary directory (live progress in the
  same status message) and sends it as a Telegram document.
- **Folder/list shares** (GoFile, Pixeldrain lists, archive.org items) deliver
  up to 10 sendable files per request, each as its own document.
- Password-protected GoFile shares: `/gofile <url> <password>`.
- **One active transfer globally**, at most **8 accepted jobs** including the
  active job, and one queued/running job per user. Excess requests receive a
  busy response rather than allocating unlimited waiting tasks.
- **ZIP extraction**: upload a ZIP document or use `/extract <url>`, then tap
  **Extract ZIP**. Both input and total output are capped at **20 MB**.
- **Responsive controls**: `/status`, `/cancel`, queue positions, download
  speed/ETA, styled buttons and copy-link buttons. No paid Telegram features.
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
| `/extract <url> [share-password]` | Extract a ZIP, ≤20 MB archive AND output; alias `/unzip` |
| `/status` | Queue, active transport, configured limits, and your job phase |
| `/cancel` | Stop your queued/running transfer or metadata lookup |
| `/links <url> [password]` | Fresh host download URLs, without Telegram upload size caps |
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
| `ENABLE_MTPROTO` | no | Set `true` for built-in up-to-2 GB bot uploads. No separate Bot API server needed. Default `false`. |
| `API_ID` | for MTProto | Your Telegram application ID from [my.telegram.org/apps](https://my.telegram.org/apps). Positive integer. |
| `API_HASH` | for MTProto | Your Telegram application hash from the same page. Keep it secret. |
| `TELEGRAM_API_URL` | no | Origin of your **Local Bot API** server started with `--local`, e.g. `http://telegram-bot-api:8081`. Enables a hard 2,000,000,000-byte per-file cap. Not a switch that upgrades the public API. |
| `DOWNLOAD_DIR` | no | Temporary download directory. Local API mode needs both processes to access the same absolute path, with matching permissions. |
| `PORT` | no | Port for the `/health` server (default `10000`); Render sets this automatically. |

Other operational limits are constants in [`gofile_bot/config.py`](gofile_bot/config.py).
Cloud-only mode uses the existing 48 MiB safety cap and 30-minute timeout.
MTProto and Local API modes use a decimal **2 GB (2,000,000,000 bytes)** cap
and a 6-hour job timeout. Every mode permits only one active transfer. Choose one large-file transport, not both.
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

Telegram allows one poller per `BOT_TOKEN`. A second copy on the same machine
refuses to start (advisory lock in the temp directory, released automatically
when the process exits) instead of fighting the first one for updates; see
[One poller per bot token](#one-poller-per-bot-token-409-conflict).

---

## ZIP extraction and responsive controls

**Option 1:** Send the bot a `.zip` document, then tap **📦 Extract ZIP**.
**Option 2:** Use `/extract <public-ZIP-url>` (alias `/unzip`). Supported host
shares work too; an optional password here unlocks the **share**, not an
encrypted ZIP. Normal ZIP link cards also include an Extract button when
eligible. Files are sent individually, with their relative paths in captions.

Safety is intentionally strict:

- The downloaded ZIP must be **≤20,000,000 bytes** and all extracted files
  together must also be **≤20,000,000 bytes**. These are decimal MB, not MiB.
- At most **20 files / 100 total entries**; only stored and deflated ZIPs.
- Reject corrupt, encrypted, split/ZIP64, traversal (`../`, absolute/drive paths),
  symlink/special-file and duplicate-path archives. Central-directory allocation
  is checked before `ZipFile` parses it. CRC errors also fail the job.
- Nested ZIPs are sent as files, **never recursively extracted**. Nothing from
  an archive is executed. This is not an antivirus scan of the delivered files.
- Extraction runs in a separate process, not on the bot's event loop. Linux
  enforces a **128 MiB address-space cap and 10-second CPU cap**; the parent
  enforces a **20-second deadline**, kills/reaps cancelled workers, and removes
  partial output. Non-POSIX platforms lack the OS resource limits; Docker/Linux
  is the supported target for the strongest protections.
- ZIP work shares the **same single transfer slot** as downloads, so multiple
  users cannot launch decompression processes simultaneously.

Use `/status` to see your job phase and current queue position. Use `/cancel`
or the red Cancel button to stop queued work immediately or interrupt an active
transfer (including stalled network I/O and extraction). A cancelled upload
cannot retract a document that Telegram has already accepted.

## 512 MB resource policy and overload behavior

The bot deliberately prioritizes staying responsive over running everyone's
files simultaneously:

- **1 active transfer + up to 7 queued**, with one job/user. The full queue
  returns a busy response; queued requests expire after 10 minutes. Queued jobs
  do not create temporary file trees or start downloads.
- **2 background metadata tasks** at most. Handlers return promptly; `/ping`,
  `/status` and `/cancel` do not wait for a multi-GB transfer. Update handling
  is sequential with a bounded 32-update buffer, not an unbounded fan-out of
  semaphore-waiting tasks.
- In-memory cards, per-user cards, folder expansions, metadata response bytes,
  Telegram peer caches and HTTP pools are bounded. Oversized metadata is
  refused. Metadata requests require identity content encoding to avoid
  decompression before a size check.
- Source transfers use 64 KiB buffers; MTProto uploads use 512 KiB parts.
  Filesystem writes run off the event loop. Status edits are throttled even
  when Telegram fails or requests a retry delay. No Redis/database/media
  conversion/paid broadcasts are required.
- Shutdown stops admission and cancels work before closing clients rather than
  waiting hours for a large upload. Temporary data is cleaned before releasing
  the active slot; old crash leftovers are swept at startup.

**The hard bot-download ceiling is exactly 2,000,000,000 bytes.** It cannot be
raised with an environment setting or a larger caller-supplied download limit.
Known oversized files are rejected before transfer; oversized Content-Length
is rejected before reading the body; unknown/misreported sizes are counted
while streaming, aborted at the cap, and partial files deleted. The same cap
applies to the large-file folder budget and MTProto uploads. Cloud-only mode
still has its lower 48 MiB Telegram-delivery cap. `/links` does not transfer a
file through this server and therefore is not a way to enlarge its upload cap.

**Storage matters:** use a real **disk-backed `DOWNLOAD_DIR`**, with at least
~3 GB free for a near-limit file. A 2 GB temporary file on a RAM-backed `/tmp`
can OOM a 512 MB host despite chunked network I/O. Linux tmpfs/ramfs mounts are
refused for larger work; Docker defaults to `/app/downloads` on its normal
filesystem. Do not mount that path as tmpfs. On other platforms verify the
storage type yourself; memory caps alone cannot make RAM storage safe. The
separate Local API server's cache/RAM is outside the bot's own budget.

### Reproducible offline overload check

```bash
python -m scripts.resource_smoke  # Linux; no credentials or external requests
```

The check sets a **512 MiB process address-space limit**, wires the MTProto
libraries, submits **2,000 transfer requests + 1,000 metadata requests**, and
actually extracts eight ZIPs to **20 MB each** using the isolated worker. It
uses a fake Telegram send transport and checks the real `/health` handler
throughout. One observed run in this sandbox:

| Measurement | Observed |
| --- | --- |
| Accepted transfer jobs / excess busy responses | 8 / 1,992 |
| Accepted metadata jobs / excess busy responses | 2 / 998 |
| Peak active transfers | 1 |
| Parent peak RSS | ~80 MiB |
| Conservative parent + largest child peak RSS sum | ~159 MiB |
| Maximum health heartbeat gap | ~13 ms |

The worker separately has a 128 MiB address-space limit. The RSS sum is a
conservative estimate from OS process peak counters (not simultaneous samples
or a container-wide cgroup measurement). This is **not** a live multi-GB
Telegram/bandwidth benchmark or a guarantee against every provider, network,
disk, host OOM condition or unlimited adversarial traffic. Overload is shed;
accepted work remains bounded. Provision the host appropriately and verify a
real large upload after deploying.

## Telegram API compatibility and free features

The runtime requires **python-telegram-bot 22.8+ (below 23)**; the verified 22.8
SDK declares **Bot API 10.0** support ([upstream release](https://github.com/python-telegram-bot/python-telegram-bot/releases/tag/v22.8)).
The UI uses supported free inline button styles, copy-text buttons, callbacks,
progress/status edits and document delivery. These require no Stars or Premium
subscription. Hosting, storage, egress and source-host quotas are separate costs.

Bot API versions aren't plugins to enable all at once. **10.1/10.2/10.3 support
was not verified** from the available upstream material; the bot doesn't invent
fields, raise size limits based on version numbers, or use unrelated paid
features. Newer cloud-server fields can coexist with this SDK, but upgrading
and testing the SDK is required before claiming newer-version coverage. Older
Local API servers should be upgraded for modern button-field compatibility.

## Download links and quick smoke tests

These public, small GitHub ZIP archives were successfully resolved, downloaded,
and checked as valid ZIPs with this downloader on 2026-10-20:

```text
/download https://codeload.github.com/octocat/Hello-World/zip/refs/heads/master
/download https://codeload.github.com/octocat/Spoon-Knife/zip/refs/heads/main
```

To download on your device instead of sending a document through Telegram:

```text
/links https://codeload.github.com/octocat/Hello-World/zip/refs/heads/master
/links https://gofile.io/d/YOUR_REAL_SHARE_ID optional-password
```

`/links` resolves the share afresh and returns up to 10 file URLs. It does **not**
download, cache or rehost the files, so Telegram's upload cap does not apply.
Host links can expire, be IP-bound, require a browser session/cookie, or be
revoked. GoFile in particular may require opening the original share in your
browser; the bot never exposes its account cookie. These are host-provided
links, **not guaranteed permanent or anonymous links**. Run `/links` again to
refresh. Placeholder IDs in `/supported` examples are syntax examples, not
live files.

## Large files: up to 2 GB in Telegram

| Transport | Telegram limit | This bot |
| --- | --- | --- |
| Public/cloud Bot API | 50 MB upload | 48 MiB safety cap (default) |
| Local Bot API with `--local` | Up to 2000 MB upload | Supported; hard 2,000,000,000-byte cap |
| MTProto bot account | Generally up to 2 GB/file | **Supported in-process**; hard 2,000,000,000-byte cap |
| MTProto Premium **user** account | Up to 4 GB/file | Not implemented; requires a different user-account/session architecture |

A bot token does not become Premium and cannot get 4 GB uploads just by
adding MTProto. Host downloads are separately limited by the host and server
disk/bandwidth. The current bot intentionally keeps a per-file cap rather than
pretending it supports unlimited files.

### Recommended: built-in MTProto mode (no extra server)

This mode downloads the source file to temporary disk, then sends it as a
Telegram document using **the same bot** via MTProto. Commands and buttons
continue through normal Bot API polling. It does not merely return a link.

1. Obtain your own Telegram application `api_id` and `api_hash` privately at
   [my.telegram.org/apps](https://my.telegram.org/apps).
2. Add these variables in your host's secret Environment settings or `.env`:

   ```ini
   BOT_TOKEN=your-existing-bot-token
   ENABLE_MTPROTO=true
   API_ID=your-numeric-api-id
   API_HASH=your-32-character-api-hash
   ```

   Replace the example values with your actual values **on your host**, not in
   Git or chat. Keep `TELEGRAM_API_URL` unset. No phone-login code, Premium
   account, session string, cloud `logOut`, or separate API server is needed.
3. Rebuild/redeploy to install the updated requirements (Telethon + cryptg).
   On Render: edit Environment, save, and redeploy. API credentials on their
   own do not enable the feature; `ENABLE_MTPROTO=true` is required.
4. Send `/start`, then `/download <public-file-url>`, and tap **DOWNLOAD**.
   `/help` shows the active file-size cap. Startup logs confirm MTProto uploads
   are enabled. Invalid/missing credentials fail startup instead of quietly
   falling back to small-file mode.

Provide **at least ~3 GB of free disk** for a near-2 GB file, adequate bandwidth,
and a host that stays awake. The source download is streamed in 64 KiB chunks
and MTProto uploads in at most 512 KiB parts, not a whole-file RAM buffer.
Only one large transfer runs at a time; upload progress and Cancel remain
available. This doesn't bypass source-host restrictions, quotas or dead links.
Files larger than the configured cap are rejected, not split automatically.

The MTProto authorization is held only in memory and recreated from the bot
token on restart; no `.session` file is written. It receives Telegram updates
for its chat/access-hash cache, but never handles commands a second time. If a
chat isn't known after a restart, send `/start` and the link again. Bot-token
login only: it cannot upload as your personal account or get 4 GB Premium
user-account uploads. Network/Telegram failures aren't blindly retried as
whole sends, to avoid duplicate large documents.

Tests mock Telegram authorization and upload requests; a real multi-GB upload
has **not** been verified in this sandbox. Your deployment needs outbound
MTProto connectivity to Telegram, as well as normal HTTPS connectivity.

### Enable Local Bot API mode (alternative)

1. Build/install the official [Telegram Bot API server](https://github.com/tdlib/telegram-bot-api#installation).
   It needs an operator-owned Telegram `api_id` and `api_hash` as described in
   that project's documentation. These configure the **server**, not this bot;
   do not send credentials or session files in chat.
2. Stop the bot and call `logOut` on the **cloud** Bot API once before migrating
   the token to your local server ([official migration instructions](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server)).
   For example, with `BOT_TOKEN` already exported in your shell:

   ```python
   import asyncio, os
   from telegram import Bot

   async def logout():
       async with Bot(os.environ["BOT_TOKEN"]) as bot:
           await bot.log_out()

   asyncio.run(logout())
   ```

3. Start the server in `--local` mode on a private host/network. Example server
   command (run under the same service user as the bot):

   ```bash
   telegram-bot-api --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" \
     --local --http-port=8081 --dir=/srv/telegram-bot-api
   ```

4. Configure this bot and restart it:

   ```ini
   TELEGRAM_API_URL=http://telegram-bot-api:8081
   DOWNLOAD_DIR=/downloads
   ```

   Use your local server's actual resolvable hostname. If both processes run
   on one host, a loopback origin is also appropriate for this **server-side**
   connection. Never point this setting at `api.telegram.org` expecting a limit
   increase. The setting must be an origin only (no `/bot`, token, or query).

5. Both processes must see `/downloads` at the **same absolute path**, and the
   local server must be able to read the files. With containers, mount the same
   volume at `/downloads` in both containers and run them with compatible UIDs
   (job directories are private). Keep the Local API port private: it is not
   intended to be an unauthenticated public upload service.

Local uploads pass a file path to Telegram's local server instead of loading
multi-GB multipart bodies into the bot's RAM. Cloud uploads also avoid eager
whole-file buffering. Downloads remain 64 KiB chunks. Files are removed after
each upload, including failed jobs. Large-file folders have a 2,000,000,000-byte aggregate
budget; cloud folders retain the 300 MiB aggregate budget, including actual
bytes for unknown-size files. Metadata size is checked before downloading and
the stream is capped even when metadata is missing or wrong. The bot checks
available disk space before each file, but other processes can still exhaust it.

**Deployment note:** a 512 MB claim applies to the bot alone, not to a combined
Local API server deployment. Provision extra RAM for the API server and several
GB of free disk for temporary files and the server's own cache. Merely setting
these variables on Render without running the Local API server/shared storage
will not work. Full live Telegram/local-server delivery must be verified in
your deployment; unit tests do not upload a real 2 GB file.

---

## Deploying to Render (or Docker)

Because the bot uses Telegram **long polling** and also serves `GET /health`
on `0.0.0.0:$PORT`, it runs cleanly as a Render **Web Service** using the
included [`Dockerfile`](Dockerfile) and [`render.yaml`](render.yaml). A
512 MB can run the bounded bot workload; large files still require real disk
space and bandwidth. A separate Local Bot API server needs additional resources.

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

### One poller per bot token (`409 Conflict`)

Telegram delivers updates to a **single** `getUpdates` consumer per bot token.
A zero-downtime deploy starts the new container before stopping the old one,
so for a few seconds both poll and Telegram answers the loser with:

```
telegram.error.Conflict: Conflict: terminated by other getUpdates request;
make sure that only one bot instance is running
```

That message is expected during a rollout and clears by itself when the old
container exits. The bot treats it accordingly:

- the first conflict is **one warning** with the likely causes, then quiet
  retries — no per-attempt error traceback;
- if it persists (a second Render service/deploy, an old container that
  ignored shutdown, or a local copy sharing `BOT_TOKEN`), a reminder follows
  every minute and a single `CRITICAL` message after five minutes;
- two bot processes on the *same* host are refused at startup by an advisory
  lock in the temp directory, so a local copy cannot silently fight the
  deployed one.

The instance that keeps losing stays alive but idle, and takes over
automatically as soon as the duplicate stops.

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
  (MediaFire). Google Drive now downloads through
  `drive.usercontent.google.com` with `confirm=t`, so files above the
  virus-scan threshold no longer stop at an HTML interstitial. OneDrive
  personal shares use the anonymous *Badger* token + shares API that the
  OneDrive web client itself uses; SharePoint links use `?download=1`.
  Neither flow signs in to a Microsoft or Google account.
- **SSRF:** every provider pins downloads to its own host allowlist and
  re-validates each redirect hop. The direct-link provider additionally
  requires the host to resolve *only* to globally-routable IPs (private,
  loopback, link-local and reserved ranges are refused, as are URLs with
  embedded credentials). Residual DNS-rebinding risk is the standard one for
  URL fetchers; set `ENABLE_DIRECT_DOWNLOADS=false` if you don't need it.
- **Rate limits are respected**: bounded exponential backoff on GoFile
  `error-rateLimit`, one active download per user, and a global cap of
  one active transfer and a bounded admission queue.

Please only download content you have the right to access, and respect each
service's terms.

## Limits

| Limit | Value | Where to change |
| --- | --- | --- |
| Max file size | Cloud-only: 48 MiB; MTProto/Local API: 2,000,000,000 bytes | `Settings.max_file_bytes` |
| Max files per folder/list | 10 | `MAX_FOLDER_FILES` |
| Max total per folder/list | Cloud-only: 300 MiB; MTProto/Local API: 2,000,000,000 bytes | `MAX_FOLDER_TOTAL_BYTES` / transport cap |
| Active transfers (all modes, downloads + ZIP extraction) | 1 | `MAX_CONCURRENT_DOWNLOADS` / app wiring |
| Accepted transfer jobs (active + queued) | 8, one per user | `MAX_PENDING_TRANSFERS` |
| Queue waiting deadline | 10 minutes | `QUEUE_TIMEOUT` |
| ZIP compressed / total expanded bytes | 20,000,000 each (decimal 20 MB) | `MAX_ZIP_BYTES` |
| ZIP output files / total archive entries | 20 / 100 | `MAX_ZIP_FILES` / `MAX_ZIP_ENTRIES` |
| ZIP worker limits (Linux) | 128 MiB address space; 10 CPU seconds; 20 seconds wall time | `zip_worker.py` / `ZIP_TIMEOUT` |
| Cached file cards | 64 total, at most 3 idle cards/user | `MAX_STORED_JOBS` / `MAX_USER_CARDS` |
| Metadata response / stored folder file count | 1 MiB / first 50 | `MAX_METADATA_BYTES` / `MAX_RESOLVED_FILES` |
| Metadata tasks / pending update buffer | 2 / 32 | `MetadataTasks` / `MAX_UPDATE_QUEUE` |
| Max job duration | Cloud-only: 30 min; MTProto/Local API: 6 hours | `Settings.job_timeout` |

Running a [Local Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
allows larger uploads — follow the large-file setup above. Do not just raise
`MAX_FILE_BYTES` against the cloud API.

---

## Project structure

```
.
├── gofile_bot/
│   ├── __main__.py            # CLI entry point: python -m gofile_bot
│   ├── app.py                 # Application assembly + lifecycle (bot + /health)
│   ├── config.py              # .env settings + operational constants
│   ├── metadata.py            # Bounded metadata reads (1 MiB)
│   ├── errors.py              # Typed errors with friendly user messages
│   ├── health.py              # Tiny aiohttp server: GET /health -> 200 OK
│   ├── polling.py             # Single-poller lock + 409 Conflict handling
│   ├── downloaders/           # One provider module per source
│   │   ├── __init__.py        # build_registry(): routing order
│   │   ├── base.py            # Downloader interface + registry
│   │   ├── net.py             # SSRF guard, redirect-safe streaming & probing
│   │   ├── telegram_zip.py    # Streaming uploaded ZIP retrieval
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
│   │   ├── mtproto.py         # Optional bot-token MTProto uploader (~2 GB)
│   │   ├── resources.py       # Admission limits, cancellation and async disk writes
│   │   ├── zip_extract.py     # Isolated ZIP worker lifecycle
│   │   ├── zip_worker.py      # Safe bounded ZIP extraction (stdlib only)
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
python -m pytest              # run the test suite
ruff check gofile_bot tests   # lint (optional)
```

The tests cover URL/SSRF validation, website-token derivation, payload
parsing, every downloader's link parsing/routing, redirect safety, the GoFile
client (rate-limit backoff, token-window retry, guest token refresh,
streaming downloads, cancellation, size caps), job management, the global
concurrency cap, MTProto configuration/authorization, chunked-upload routing,
cancellation/timeout cleanup, transfer orchestration and the Telegram handlers — all
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
| OneDrive says it "needs a Microsoft sign-in" | The link isn't shared as *Anyone with the link* (or its 90-day anonymous share expired). Re-share the file publicly and send the new link. |
| "GoFile is rate-limiting requests" | Wait a few minutes; heavy users can set `GOFILE_API_TOKEN`. |
| "too big for Telegram uploads" | The info card now says so before you tap anything and hides the Download button. Configure built-in MTProto mode (recommended), Local Bot API mode, or use `/links <url>` (see Large files). |
| "MTProto bot login failed" | Check `ENABLE_MTPROTO`, `API_ID`, `API_HASH`, the existing bot token, and outbound Telegram MTProto connectivity. |
| "uploader hasn't seen this chat yet" | Send `/start` and then the link again; the in-memory peer cache resets on restart. |
| "This button has expired" | Jobs live in memory for 24 h (and across restarts they reset) — send the link again. |
| "Queued" | One transfer is active; `/status` shows your position. `/cancel` removes your job. Maximum wait: 10 min. |
| "at its safe capacity" | The bounded queue/metadata slots are full; retry later. |
| "ZIP rejected" | Check the 20 MB input/output cap, supported compression, entry count and safe filenames. |
| "disk-backed storage" | Set `DOWNLOAD_DIR` to real disk storage, not tmpfs; leave enough free space. |
| `Conflict: terminated by other getUpdates request` | Two processes poll the same bot token. Deploy overlap clears in seconds by itself; if it keeps repeating, stop the duplicate — a second Render service/deploy, an old container that ignored shutdown, or a local `python -m gofile_bot`. See [One poller per bot token](#one-poller-per-bot-token-409-conflict). |
| `startup aborted: Another bot process is already polling` | A second copy on the same host uses the same `BOT_TOKEN`. Stop it (the lock is released automatically when that process exits). |
| GoFile downloads suddenly fail with "not accepting requests" | GoFile may have rotated the salt in its web client; update `GOFILE_WT_SALT` with the current value from `gofile.io`'s `wt` script, or set a Premium `GOFILE_API_TOKEN`. |
| MediaFire/Kraken-style host stops working | Page-scrape providers break when the site changes its HTML — open an issue with a sample link. |

## Credits

Made By @Yorichiiprime

## License

See [LICENSE](LICENSE).
