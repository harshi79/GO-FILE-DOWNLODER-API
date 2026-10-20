"""Configuration: environment-backed settings and operational constants.

Secrets and deployment-specific values come from the environment (optionally
loaded from a ``.env`` file). Everything else — timeouts, size caps, rate
limiting knobs — lives here as plain constants so they are easy to audit and
adjust when running the bot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# GoFile API
# ---------------------------------------------------------------------------

GOFILE_API_BASE = "https://api.gofile.io"
GOFILE_ACCOUNTS_URL = f"{GOFILE_API_BASE}/accounts"
GOFILE_CONTENT_URL = f"{GOFILE_API_BASE}/contents/{{content_id}}"

#: Only hosts below this domain are ever contacted for downloads. User input
#: never influences the request target directly; download URLs come from the
#: GoFile API response and are re-validated against this allowlist before use
#: (SSRF protection).
GOFILE_DOWNLOAD_HOST_SUFFIX = ".gofile.io"
GOFILE_WEB_BASE = "https://gofile.io"

#: User-Agent hashed into the website token. Must match the ``User-Agent``
#: header sent on API requests.
GOFILE_USER_AGENT = "Mozilla/5.0"

#: Language hashed into the website token. Must match the ``X-BL`` header.
GOFILE_LOCALE = "en-US"

#: GoFile rotates its client-side website token on this cadence (seconds).
GOFILE_WT_WINDOW_SECONDS = 14_400

#: Default salt embedded in GoFile's public web client token generator.
DEFAULT_GOFILE_WT_SALT = "12af056dacea0b"

# ---------------------------------------------------------------------------
# HTTP behaviour
# ---------------------------------------------------------------------------

#: How long to wait for a metadata response before giving up (seconds).
METADATA_TIMEOUT = 30

#: Per-chunk read timeout while streaming a file (seconds). The overall job
#: timeout below bounds the total time.
DOWNLOAD_SOCK_READ_TIMEOUT = 120

#: Upper bound for a whole download+upload job (seconds).
JOB_TIMEOUT = 30 * 60

#: Attempts for GoFile API calls that fail transiently (rate limit, timeout).
API_MAX_ATTEMPTS = 4

#: Exponential-ish backoff base for error-rateLimit retries (seconds).
RATE_LIMIT_BACKOFF_BASE = 3

#: Maximum number of concurrent GoFile API requests across all users.
API_CONCURRENCY = 3

# ---------------------------------------------------------------------------
# Telegram limits & bot behaviour
# ---------------------------------------------------------------------------

#: Hard Telegram Bot API upload limit is 50 MB. Stay a bit below it so the
#: multipart envelope also fits. Raise this only when running a Local Bot API
#: server (which allows up to 2 GB).
MAX_FILE_BYTES = 48 * 1024 * 1024
# Absolute policy limit: decimal 2 GB. Lower transport limits still apply.
HARD_MAX_FILE_BYTES = 2_000_000_000
LARGE_MAX_FILE_BYTES = HARD_MAX_FILE_BYTES
# Decimal MB for the user-facing ZIP policy, on input AND expanded output.
MAX_ZIP_BYTES = 20_000_000
MAX_ZIP_FILES = 20
MAX_ZIP_ENTRIES = 100
ZIP_TIMEOUT = 20
MAX_PENDING_TRANSFERS = 8
QUEUE_TIMEOUT = 10 * 60
MAX_STORED_JOBS = 64
MAX_USER_CARDS = 3
MAX_METADATA_BYTES = 1024 * 1024
MAX_RESOLVED_FILES = 50
MAX_UPDATE_QUEUE = 32
LOCAL_MAX_FILE_BYTES = LARGE_MAX_FILE_BYTES

#: Folder shares: caps that keep a single button press from turning into an
#: unbounded multi-gigabyte transfer.
MAX_FOLDER_FILES = 10
MAX_FOLDER_TOTAL_BYTES = 300 * 1024 * 1024

#: How deep nested folders are walked when resolving a share (GoFile allows
#: up to 16; we keep it small on purpose).
MAX_FOLDER_DEPTH = 3

#: Minimum seconds between two edits of the same status message.
PROGRESS_EDIT_INTERVAL = 3.0

#: Don't retry uploads on transient Telegram errors more than this many times.
UPLOAD_MAX_ATTEMPTS = 2

#: How long a stored job keeps its Download button working (seconds).
JOB_TTL = 24 * 60 * 60

#: Prefix for per-job temporary directories.
TMP_DIR_PREFIX = "gofile-bot-"

#: Directory used for temporary downloads. Defaults to the system temp dir.
DOWNLOAD_CHUNK_SIZE = 64 * 1024

#: Port for the /health HTTP server. Platforms like Render inject ``PORT``.
DEFAULT_HEALTH_PORT = 10_000

# ---------------------------------------------------------------------------
# Additional downloaders (direct links, Google Drive, Dropbox, OneDrive,
# MediaFire, Pixeldrain, Internet Archive)
#
# All of them work the same low-RAM way: fetch small JSON/HTML metadata to
# obtain a direct URL, then stream the file to disk in DOWNLOAD_CHUNK_SIZE
# chunks. Nothing loads a whole file (or a media toolchain) into memory, so
# the bot stays comfortable on a 512 MB instance.
# ---------------------------------------------------------------------------

#: Browser-like UA used by the generic (non-GoFile) downloaders.
GENERIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: Redirect hops followed manually (each hop is re-validated) by the generic
#: HTTP downloaders.
DIRECT_MAX_REDIRECTS = 5

#: Maximum downloads running at the same time across *all* users. Each one
#: streams with a small fixed buffer, but on a 512 MB box a hard cap keeps
#: worst-case memory (and bandwidth/disk churn) predictable.
MAX_CONCURRENT_DOWNLOADS = 1


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from the environment."""

    bot_token: str = field(repr=False)
    gofile_api_token: str | None = None
    gofile_wt_salt: str = DEFAULT_GOFILE_WT_SALT
    #: Allow arbitrary direct http(s) file links (SSRF-guarded). Set
    #: ENABLE_DIRECT_DOWNLOADS=false to accept only known file hosts.
    enable_direct_downloads: bool = True
    #: Operator-owned Bot API endpoint, e.g. http://telegram-bot-api:8081.
    telegram_api_url: str | None = None
    download_dir: str | None = None
    enable_mtproto: bool = False
    api_id: int | None = None
    api_hash: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.enable_mtproto:
            if self.telegram_api_url:
                raise ValueError("Choose ENABLE_MTPROTO or TELEGRAM_API_URL, not both")
            if not isinstance(self.api_id, int) or isinstance(self.api_id, bool) or self.api_id <= 0:
                raise ValueError("API_ID must be a positive integer when ENABLE_MTPROTO=true")
            if (not self.api_hash or len(self.api_hash) != 32
                    or any(c not in "0123456789abcdefABCDEF" for c in self.api_hash)):
                raise ValueError("API_HASH must be the 32-character hex hash from my.telegram.org")
        if self.telegram_api_url:
            parsed = urlparse(self.telegram_api_url)
            if (
                parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in ("", "/")
            ):
                raise ValueError(
                    "TELEGRAM_API_URL must be an http(s) server origin, "
                    "without a path or credentials"
                )
            # Accessing .port also rejects malformed/non-numeric TCP ports.
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError("TELEGRAM_API_URL has an invalid port")

    @property
    def max_file_bytes(self) -> int:
        return LARGE_MAX_FILE_BYTES if self.large_files_enabled else MAX_FILE_BYTES

    @property
    def large_files_enabled(self) -> bool:
        return bool(self.telegram_api_url or self.enable_mtproto)

    @property
    def max_concurrent_downloads(self) -> int:
        return MAX_CONCURRENT_DOWNLOADS

    @property
    def job_timeout(self) -> int:
        return 6 * 60 * 60 if self.large_files_enabled else JOB_TIMEOUT


def get_health_port() -> int:
    """Port for the /health server (``PORT`` env var, default 10000).

    Hosting platforms such as Render inject ``PORT`` automatically. The value
    must be a valid TCP port.
    """
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        return DEFAULT_HEALTH_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"PORT must be a number, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"PORT must be in 1..65535, got {port}")
    return port


def load_settings(env_file: str | None = ".env") -> Settings:
    """Build :class:`Settings` from the environment.

    Loads ``.env`` (if present) via python-dotenv without overriding variables
    that are already exported. Raises :class:`ValueError` when the required
    ``BOT_TOKEN`` is missing.
    """
    if env_file:
        try:
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - dotenv is a hard dependency
            pass
        else:
            if os.path.isfile(env_file):
                load_dotenv(env_file, override=False)

    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError(
            "BOT_TOKEN is not set. Put your @BotFather token in .env "
            "(see .env.example) or export BOT_TOKEN."
        )

    api_token = os.environ.get("GOFILE_API_TOKEN", "").strip() or None
    salt = os.environ.get("GOFILE_WT_SALT", "").strip() or DEFAULT_GOFILE_WT_SALT

    raw_api_id = os.environ.get("API_ID", "").strip()
    try:
        api_id = int(raw_api_id) if raw_api_id else None
    except ValueError as exc:
        raise ValueError("API_ID must be a positive integer") from exc

    return Settings(
        bot_token=bot_token,
        gofile_api_token=api_token,
        gofile_wt_salt=salt,
        enable_direct_downloads=_env_bool("ENABLE_DIRECT_DOWNLOADS", True),
        telegram_api_url=os.environ.get("TELEGRAM_API_URL", "").strip().rstrip("/") or None,
        download_dir=os.environ.get("DOWNLOAD_DIR", "").strip() or None,
        enable_mtproto=_env_bool("ENABLE_MTPROTO", False),
        api_id=api_id,
        api_hash=os.environ.get("API_HASH", "").strip() or None,
    )
