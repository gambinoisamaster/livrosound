"""Library info read straight from the files: subjects, series, dates, pages, covers."""

from __future__ import annotations

import zipfile

import pymupdf
from PySide6.QtGui import QColor, QImage

from livrosound import bookinfo

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Night</dc:title><dc:identifier id="id">x</dc:identifier>
  <dc:subject>Fiction; Russia -- History</dc:subject><dc:subject>fiction</dc:subject>
  <dc:date>1877-01-01</dc:date>
  <meta name="calibre:series" content="Russian Novels"/><meta name="calibre:series_index" content="2"/>
  <meta name="cover" content="cov"/>
</metadata>
<manifest><item id="cov" href="images/c.png" media-type="image/png"/></manifest><spine/>
</package>"""

CONTAINER = """<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>"""


def png_bytes(qapp, color="#336699", size=(300, 450)) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def test_epub_metadata_and_cover(tmp_path, qapp):
    path = tmp_path / "night.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER)
        zf.writestr("OEBPS/content.opf", OPF)
        zf.writestr("OEBPS/images/c.png", png_bytes(qapp, size=(400, 1200)))
    info = bookinfo.read_info(path, "epub", words=55_000)
    assert info.subjects == ["Fiction", "Russia", "History"]
    assert (info.series, info.series_index, info.published) == ("Russian Novels", 2.0, "1877-01-01")
    assert info.pages == 200  # 55,000 words at 275 a page
    saved = bookinfo.save_cover("bk", info.cover)
    assert saved == bookinfo.cover_path("bk") and saved.is_file()
    image = QImage(str(saved))
    assert image.height() == bookinfo.COVER_HEIGHT and image.width() == 200


def test_pdf_pages_date_and_first_page_cover(tmp_path, qapp):
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page(width=300, height=450).insert_text((40, 60), "Hello")
    doc.set_metadata({"keywords": "Style, Grammar", "creationDate": "D:20030501120000Z"})
    path = tmp_path / "b.pdf"
    doc.save(path)
    info = bookinfo.read_info(path, "pdf")
    assert info.pages == 3 and info.subjects == ["Style", "Grammar"] and info.published == "2003-05-01"
    assert info.cover and QImage.fromData(info.cover).height() == bookinfo.COVER_HEIGHT


def test_broken_or_plain_files_give_empty_info(tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    info = bookinfo.read_info(bad, "epub")
    assert info == bookinfo.BookInfo()
    text = tmp_path / "t.txt"
    text.write_text("hi")
    assert bookinfo.read_info(text, "txt", words=600).pages == 2
    assert bookinfo.save_cover("bk", b"garbage") is None and bookinfo.save_cover("bk", b"") is None
