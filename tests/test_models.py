"""Content payload parsing tests."""

import pytest

from gofile_bot.errors import PasswordIncorrectError, PasswordRequiredError
from gofile_bot.gofile.models import (
    RemoteFile,
    RemoteFolder,
    check_password_state,
    collect_files,
    parse_child,
    parse_file,
)


def test_parse_file_basic():
    file = parse_file(
        {
            "id": "fid",
            "type": "file",
            "name": "doc.pdf",
            "size": 1234,
            "mimetype": "application/pdf",
            "link": "https://store-1.gofile.io/download/web/fid/doc.pdf",
            "md5": "abc",
        }
    )
    assert isinstance(file, RemoteFile)
    assert file.name == "doc.pdf"
    assert file.size == 1234
    assert file.mimetype == "application/pdf"
    assert file.link.endswith("/doc.pdf")
    assert file.md5 == "abc"


def test_parse_file_builds_link_from_server():
    file = parse_file(
        {
            "id": "fid",
            "type": "file",
            "name": "a b.bin",
            "size": 10,
            "serverSelected": "store-9",
        }
    )
    assert file.link == "https://store-9.gofile.io/download/web/fid/a%20b.bin"


def test_parse_file_without_link_or_server():
    file = parse_file({"id": "fid", "type": "file", "name": "x", "size": 1})
    assert file.link == ""


def test_parse_child_folder_shell():
    child = parse_child({"id": "sub", "type": "folder", "name": "Sub"})
    assert isinstance(child, RemoteFolder)
    assert child.children == ()


def test_collect_files_flattens():
    f1 = parse_file({"id": "1", "type": "file", "name": "a", "size": 1, "link": "https://x.gofile.io/1"})
    f2 = parse_file({"id": "2", "type": "file", "name": "b", "size": 2, "link": "https://x.gofile.io/2"})
    folder = RemoteFolder(id="g", name="G", children=(f1, f2))
    assert collect_files(folder) == [f1, f2]
    assert collect_files(f1) == [f1]
    assert folder.file_count == 2
    assert folder.total_size == 3


def test_password_state_public():
    check_password_state({}, password_provided=False)  # no raise
    check_password_state({"passwordStatus": "passwordOk"}, password_provided=True)


def test_password_state_required():
    with pytest.raises(PasswordRequiredError):
        check_password_state({"password": "x"}, password_provided=False)
    with pytest.raises(PasswordRequiredError):
        check_password_state(
            {"password": "x", "passwordStatus": "passwordRequired"},
            password_provided=False,
        )


def test_password_state_incorrect():
    with pytest.raises(PasswordIncorrectError):
        check_password_state(
            {"password": "x", "passwordStatus": "passwordError"},
            password_provided=True,
        )


def test_password_state_provided_no_verdict_passes():
    check_password_state({"password": "x"}, password_provided=True)
