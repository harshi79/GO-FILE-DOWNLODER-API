"""Transfer orchestration tests with fake bot/GoFile clients."""

from pathlib import Path

from gofile_bot.config import MAX_FILE_BYTES, MAX_FOLDER_FILES
from gofile_bot.errors import FileTooLargeError, JobCancelled
from gofile_bot.gofile.models import RemoteFile, RemoteFolder
from gofile_bot.services.jobs import Job, JobStore
from gofile_bot.services.transfer import (
    TransferService,
    cleanup_stale_tmpdirs,
    make_job_tmpdir,
    safe_filename,
)


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.sent: list[tuple[str, str]] = []

    async def edit_message_text(
        self,
        chat_id,
        message_id,
        text,
        reply_markup=None,
        parse_mode=None,
        disable_web_page_preview=None,
    ):
        self.edits.append(text)

    async def send_document(
        self,
        chat_id,
        document,
        filename=None,
        caption=None,
        parse_mode=None,
        read_timeout=None,
        write_timeout=None,
    ):
        self.sent.append((filename or document.name, caption or ""))


class FakeGofile:
    def __init__(self, *, fail: Exception | None = None, payload: bytes = b"data"):
        self.fail = fail
        self.payload = payload
        self.downloads: list[str] = []

    async def download_file(self, file, dest: Path, *, max_bytes, progress=None, cancel_event=None):
        if self.fail is not None:
            raise self.fail
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        dest.write_bytes(self.payload)
        self.downloads.append(file.name)
        if progress is not None:
            await progress(len(self.payload), len(self.payload))
        return len(self.payload)


def make_job(content) -> Job:
    store = JobStore()
    job = store.create(
        user_id=1,
        chat_id=2,
        status_message_id=3,
        content_id="abc",
        password=None,
    )
    job.content = content
    job.running = True
    return job


def make_file(name="doc.pdf", size=1000, fid="fid") -> RemoteFile:
    return RemoteFile(
        id=fid,
        name=name,
        size=size,
        mimetype="application/pdf",
        link=f"https://store-1.gofile.io/download/web/{fid}/{name}",
    )


async def test_single_file_success():
    bot, gofile = FakeBot(), FakeGofile()
    job = make_job(make_file())
    await TransferService(bot, gofile).run(job)
    assert bot.sent and bot.sent[0][0] == "doc.pdf"
    assert any("✅" in e for e in bot.edits)
    assert not job.running


async def test_folder_sends_each_file():
    bot, gofile = FakeBot(), FakeGofile()
    folder = RemoteFolder(
        id="g", name="Album", children=(make_file("a.pdf", fid="1"), make_file("b.pdf", fid="2"))
    )
    job = make_job(folder)
    await TransferService(bot, gofile).run(job)
    assert [s[0] for s in bot.sent] == ["a.pdf", "b.pdf"]
    assert any("2 files" in e for e in bot.edits)


async def test_folder_caps_file_count():
    bot, gofile = FakeBot(), FakeGofile()
    children = tuple(
        make_file(f"f{i:02}.pdf", size=10, fid=f"id{i}") for i in range(MAX_FOLDER_FILES + 5)
    )
    job = make_job(RemoteFolder(id="g", name="Big", children=children))
    await TransferService(bot, gofile).run(job)
    assert len(bot.sent) == MAX_FOLDER_FILES
    assert any("Skipped" in e for e in bot.edits)


async def test_folder_skips_oversized_files():
    bot, gofile = FakeBot(), FakeGofile()
    folder = RemoteFolder(
        id="g",
        name="Mixed",
        children=(
            make_file("huge.bin", size=MAX_FILE_BYTES + 1, fid="huge"),
            make_file("ok.pdf", size=10, fid="ok"),
        ),
    )
    job = make_job(folder)
    await TransferService(bot, gofile).run(job)
    assert [s[0] for s in bot.sent] == ["ok.pdf"]
    assert any("Skipped" in e and "huge.bin" in e for e in bot.edits)


async def test_error_is_reported():
    bot = FakeBot()
    gofile = FakeGofile(
        fail=FileTooLargeError(size=10**9, limit=MAX_FILE_BYTES)
    )
    job = make_job(make_file())
    await TransferService(bot, gofile).run(job)
    assert not bot.sent
    assert any("too big" in e for e in bot.edits)
    assert not job.running


async def test_cancel_before_start():
    bot, gofile = FakeBot(), FakeGofile()
    job = make_job(make_file())
    job.cancel_event.set()
    await TransferService(bot, gofile).run(job)
    assert not gofile.downloads
    assert any("cancelled" in e.lower() for e in bot.edits)


async def test_cancel_mid_download():
    bot = FakeBot()
    job = make_job(make_file())
    event = job.cancel_event

    class CancellingGofile(FakeGofile):
        async def download_file(self, file, dest, *, max_bytes, progress=None, cancel_event=None):
            event.set()
            raise JobCancelled()

    await TransferService(bot, CancellingGofile()).run(job)
    assert any("cancelled" in e.lower() for e in bot.edits)
    assert not job.running


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("a/b\\c.txt") == "c.txt"
    assert safe_filename("...") == "file"
    assert safe_filename("") == "file"
    assert safe_filename("x" * 200 + ".pdf").endswith(".pdf")
    assert len(safe_filename("x" * 200 + ".pdf")) <= 120
    assert safe_filename("na\x00me.txt") == "name.txt"


def test_tmpdir_helpers():
    d = make_job_tmpdir()
    try:
        assert d.is_dir()
        assert "gofile-bot-" in d.name
    finally:
        d.rmdir()
    assert cleanup_stale_tmpdirs(max_age_seconds=-1) >= 0


def test_tmpdir_cleanup():
    d = make_job_tmpdir()
    removed = cleanup_stale_tmpdirs(max_age_seconds=-1)
    assert removed >= 1
    assert not d.exists()


class FakeUsage:
    def __init__(self) -> None:
        self.records: list[tuple[int, dict]] = []

    async def record_download(self, user_id, **kwargs):
        self.records.append((user_id, kwargs))


async def test_successful_transfer_records_usage():
    bot, gofile = FakeBot(), FakeGofile(payload=b"data")
    usage = FakeUsage()
    job = make_job(make_file())
    await TransferService(bot, gofile, usage=usage).run(job)
    assert usage.records == [
        (
            1,
            {
                "provider": "gofile",
                "action": "download",
                "status": "ok",
                "bytes": 4,
                "duration": usage.records[0][1]["duration"],
            },
        )
    ]
    assert usage.records[0][1]["duration"] >= 0


async def test_failed_transfer_records_failure():
    bot = FakeBot()
    gofile = FakeGofile(fail=FileTooLargeError(size=10**9, limit=MAX_FILE_BYTES))
    usage = FakeUsage()
    job = make_job(make_file())
    await TransferService(bot, gofile, usage=usage).run(job)
    user_id, record = usage.records[0]
    assert user_id == 1 and record["status"] == "failed"
    assert record["bytes"] == 0


async def test_broken_usage_store_never_breaks_transfer():
    class Exploding:
        async def record_download(self, *args, **kwargs):
            raise RuntimeError("database is gone")

    bot, gofile = FakeBot(), FakeGofile()
    job = make_job(make_file())
    await TransferService(bot, gofile, usage=Exploding()).run(job)
    assert job.phase == "complete"
    assert bot.sent[0][0] == "doc.pdf"
