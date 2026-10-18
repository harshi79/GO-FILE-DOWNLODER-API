"""Message formatting tests."""

from gofile_bot.gofile.models import RemoteFile, RemoteFolder
from gofile_bot.telegram import formatting as fmt


def test_human_size():
    assert fmt.human_size(0) == "0 B"
    assert fmt.human_size(512) == "512 B"
    assert fmt.human_size(1024) == "1.0 KB"
    assert fmt.human_size(1536) == "1.5 KB"
    assert fmt.human_size(12 * 1024**2) == "12.0 MB"
    assert fmt.human_size(3 * 1024**3) == "3.0 GB"


def test_escapes_html():
    assert fmt.esc("<b>&x") == "&lt;b&gt;&amp;x"


def _file(**kw):
    data = {
        "id": "f1",
        "name": "report.pdf",
        "size": 1024,
        "mimetype": "application/pdf",
        "link": "https://store-1.gofile.io/download/web/f1/report.pdf",
    }
    data.update(kw)
    return RemoteFile(**data)


def test_info_text_file():
    text = fmt.info_text(_file())
    assert "report.pdf" in text
    assert "1.0 KB" in text
    assert "application/pdf" in text


def test_info_text_file_escapes_name():
    text = fmt.info_text(_file(name="<script>.pdf"))
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_info_text_folder():
    folder = RemoteFolder(id="g", name="Album", children=(_file(), _file(name="b.txt")))
    text = fmt.info_text(folder)
    assert "Album" in text
    assert "2 files" in text
    assert "Folder" in text


def test_progress_text_known_total():
    text = fmt.progress_text(
        filename="a.bin", received=512, total=1024, file_index=2, file_count=5
    )
    assert "50%" in text
    assert "File 2/5" in text
    assert "512 B" in text and "1.0 KB" in text


def test_progress_text_unknown_total():
    text = fmt.progress_text(filename="a.bin", received=2048, total=None)
    assert "2.0 KB" in text
    assert "%" not in text


def test_success_text():
    assert "✅" in fmt.success_text(_file())
    folder = RemoteFolder(id="g", name="A", children=(_file(),))
    assert "1 file" in fmt.success_text(folder, sent=["report.pdf"])


def test_caption():
    assert "1.0 KB" in fmt.caption_for(_file())
