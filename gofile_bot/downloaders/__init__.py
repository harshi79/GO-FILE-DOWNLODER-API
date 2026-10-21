"""Downloader providers and the registry that routes links to them.

Every provider is a low-RAM *streaming* downloader: resolve a link to a
direct URL from small JSON/HTML metadata, then stream the file to disk in
64 KB chunks. No media processing (no yt-dlp, no ffmpeg), so the whole set
stays comfortable on a 512 MB instance.
"""

from __future__ import annotations

from gofile_bot.config import Settings
from gofile_bot.downloaders.archive_org import ArchiveOrgDownloader
from gofile_bot.downloaders.base import Downloader, DownloaderRegistry
from gofile_bot.downloaders.direct import DirectDownloader
from gofile_bot.downloaders.dropbox import DropboxDownloader
from gofile_bot.downloaders.gdrive import GoogleDriveDownloader
from gofile_bot.downloaders.gofile_provider import GoFileDownloader
from gofile_bot.downloaders.mediafire import MediaFireDownloader
from gofile_bot.downloaders.onedrive import OneDriveDownloader
from gofile_bot.downloaders.pasteview import PasteviewDownloader
from gofile_bot.downloaders.pixeldrain import PixeldrainDownloader
from gofile_bot.gofile.client import GoFileClient

__all__ = [
    "Downloader",
    "DownloaderRegistry",
    "build_registry",
]


def build_registry(settings: Settings, gofile: GoFileClient) -> DownloaderRegistry:
    """Assemble the provider registry (first match wins; Direct is last)."""
    providers: list[Downloader] = [
        GoFileDownloader(gofile),
        GoogleDriveDownloader(),
        DropboxDownloader(),
        OneDriveDownloader(),
        PixeldrainDownloader(),
        MediaFireDownloader(),
        ArchiveOrgDownloader(),
        PasteviewDownloader(),
    ]
    if settings.enable_direct_downloads:
        providers.append(DirectDownloader())
    return DownloaderRegistry(providers)
