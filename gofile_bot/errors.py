"""Exception hierarchy with user-friendly messages.

Every error that can reach a Telegram user carries a short, friendly
``user_message``; technical details go to the log instead.
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for expected, user-reportable errors."""

    user_message = "Something went wrong. Please try again."

    def __init__(self, detail: str | None = None, *, user_message: str | None = None):
        self.detail = detail
        if user_message is not None:
            self.user_message = user_message
        super().__init__(detail or self.user_message)


class InvalidURLError(BotError):
    user_message = (
        "⚠️ That doesn't look like a GoFile link.\n"
        "Send it like this: /gofile https://gofile.io/d/xxxxxx"
    )


class UnsupportedShareError(BotError):
    user_message = (
        "⚠️ Only single files and folders shared via gofile.io/d/… are supported."
    )


class UnsupportedURLError(BotError):
    user_message = (
        "⚠️ I don't know how to download from that link yet.\n"
        "Send /supported to see everything I can handle."
    )


class ResolveError(BotError):
    """A non-GoFile provider could not read the user's link."""

    user_message = (
        "⚠️ Couldn't read that link. It may be deleted, private, or the site "
        "is blocking the bot right now."
    )


class PrivateContentError(BotError):
    user_message = (
        "🔒 That content is private or needs a login, so the bot can't open it.\n"
        "Only public links are supported."
    )


class RemoteDownloadError(BotError):
    """A non-GoFile provider failed while streaming a file."""

    user_message = "⚠️ The download failed. Please try again."


class DownloadTimeoutError(BotError):
    user_message = (
        "⚠️ The download took too long and was stopped. Please try again."
    )


class GoFileError(BotError):
    """Base class for GoFile-side failures."""


class ContentNotFoundError(GoFileError):
    user_message = (
        "⚠️ This link doesn't exist or is no longer available.\n"
        "Double-check the link and try again."
    )


class PasswordRequiredError(GoFileError):
    user_message = (
        "🔒 This content is password-protected.\n"
        "Send it together with the password:\n"
        "/gofile <url> <password>"
    )


class PasswordIncorrectError(GoFileError):
    user_message = "🔒 That password is not correct for this link."


class RateLimitedError(GoFileError):
    user_message = (
        "⚠️ GoFile is rate-limiting requests right now.\n"
        "Please wait a few minutes and try again."
    )


class GoFileTimeoutError(GoFileError):
    user_message = "⚠️ GoFile didn't respond in time. Please try again shortly."


class GoFileUnavailableError(GoFileError):
    user_message = (
        "⚠️ GoFile is not accepting requests from the bot at the moment.\n"
        "Please try again later."
    )


class TokenRejectedError(GoFileError):
    user_message = (
        "⚠️ GoFile rejected the bot's access token. The bot owner may need to "
        "update its configuration."
    )


class DownloadError(GoFileError):
    user_message = "⚠️ The download from GoFile failed. Please try again."


class FileTooLargeError(BotError):
    def __init__(
        self,
        detail: str | None = None,
        *,
        size: int | None = None,
        limit: int | None = None,
        user_message: str | None = None,
    ) -> None:
        from gofile_bot.telegram.formatting import human_size

        if user_message is not None:
            super().__init__(detail, user_message=user_message)
        else:
            parts = ["⚠️ This file is too big for Telegram uploads"]
            if size is not None and limit is not None:
                parts.append(f"({human_size(size)} > {human_size(limit)})")
            parts.append("Use /links <url> for host download links, or try a smaller file.")
            super().__init__(detail, user_message=" ".join(parts))
        self.size = size
        self.limit = limit


class TelegramUploadError(BotError):
    user_message = (
        "⚠️ Telegram refused the upload. Ask the bot owner to check the active "
        "upload transport and logs, or use /links <url> to download from the host."
    )


class JobCancelled(BotError):
    user_message = "❌ Download cancelled."


class JobBusyError(BotError):
    user_message = (
        "⏳ You already have a download in progress.\n"
        'Tap "Cancel" on the status message first, or wait for it to finish.'
    )


class JobExpiredError(BotError):
    user_message = "⌛ This button has expired. Send the link again."


class ServiceBusyError(BotError):
    user_message = "⏳ The bot is at its safe capacity. Please try again shortly; existing jobs are protected."


class ZipSafetyError(BotError):
    user_message = "⚠️ ZIP rejected. Both the archive and extracted contents must be at most 20 MB, with at most 20 files."
