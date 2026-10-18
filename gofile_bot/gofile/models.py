"""Typed models for GoFile content payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from gofile_bot.errors import PasswordIncorrectError, PasswordRequiredError

#: Password states reported by the GoFile contents endpoint.
_PASSWORD_OK = "passwordOk"


@dataclass(frozen=True)
class RemoteFile:
    """A single file on GoFile."""

    id: str
    name: str
    size: int
    mimetype: str
    link: str
    md5: str | None = None

    @property
    def is_file(self) -> bool:
        return True


@dataclass(frozen=True)
class RemoteFolder:
    """A folder (album) on GoFile."""

    id: str
    name: str
    children: tuple[Content, ...] = field(default_factory=tuple)

    @property
    def is_file(self) -> bool:
        return False

    def files(self) -> list[RemoteFile]:
        """Flatten the folder into its files (depth-first)."""
        result: list[RemoteFile] = []
        for child in self.children:
            if isinstance(child, RemoteFile):
                result.append(child)
            else:
                result.extend(child.files())
        return result

    @property
    def file_count(self) -> int:
        return len(self.files())

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files())


Content = RemoteFile | RemoteFolder


def collect_files(content: Content) -> list[RemoteFile]:
    """Return every file inside *content* (a file yields itself)."""
    if isinstance(content, RemoteFile):
        return [content]
    return content.files()


def _build_file_link(data: dict) -> str:
    """Best-effort download link for a file entry.

    Prefers the ``link`` field GoFile returns; falls back to constructing one
    from the assigned storage server and file id (same shape the web client
    uses). The result is still validated against the host allowlist before any
    request is made.
    """
    link = (data.get("link") or "").strip()
    if link:
        return link

    server = data.get("serverSelected") or data.get("serverChoosen")
    if not server:
        servers = data.get("servers") or []
        if isinstance(servers, list) and servers:
            server = servers[0]
    file_id = data.get("id", "")
    name = quote(data.get("name") or "file")
    if server and file_id:
        return f"https://{server}.gofile.io/download/web/{file_id}/{name}"
    return ""


def parse_file(data: dict) -> RemoteFile:
    name = str(data.get("name") or "file")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return RemoteFile(
        id=str(data.get("id") or ""),
        name=name,
        size=size,
        mimetype=str(data.get("mimetype") or "application/octet-stream"),
        link=_build_file_link(data),
        md5=(data.get("md5") or None),
    )


def parse_folder(data: dict, children: tuple[Content, ...]) -> RemoteFolder:
    return RemoteFolder(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or "folder"),
        children=children,
    )


def parse_child(data: dict) -> Content:
    """Parse a child entry (files fully; folder shells need a separate fetch)."""
    if data.get("type") == "folder":
        return parse_folder(data, children=())
    return parse_file(data)


def check_password_state(data: dict, password_provided: bool) -> None:
    """Raise the right error when the share is password-protected.

    GoFile reports protection via a ``password`` attribute and a
    ``passwordStatus`` field on the content payload.
    """
    status = data.get("passwordStatus")
    if status == _PASSWORD_OK:
        return
    if status is None:
        if "password" not in data:
            return  # ordinary public share
        if password_provided:
            return  # protected, but no verdict yet — let the fetch decide
        raise PasswordRequiredError("password-protected content")
    # Explicit non-ok verdict from GoFile.
    if password_provided:
        raise PasswordIncorrectError(f"passwordStatus={status}")
    raise PasswordRequiredError(f"passwordStatus={status}")
