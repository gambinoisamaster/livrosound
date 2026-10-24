"""EPUB ingestion: small EPUB2/EPUB3 books built here cover every rule; slow tests
read the real samples (Anna Karenina, The Age of Innocence, Wuthering Heights,
and the Internet Archive scan of Gogol's The Overcoat).

original.html is checked the way the reader view uses it: rendered by Qt, every
block's anchor must be found and the text from one anchor to the next must be
the block's text (whitespace aside).
"""

from __future__ import annotations

import hashlib
import re
import time
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from livrosound import paths
from livrosound.book import SKIPPED_ROLES, TITLE_ROLE
from livrosound.ingest import BookImportError, load_book
from livrosound.ingest.epub import parse_epub

EBOOKS = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Ebooks"

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="{opf}" media-type="application/oebps-package+xml"/></rootfiles></container>"""


def xhtml(body: str, head: str = "", body_attrs: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>t</title>{head}</head>'
        f"<body{body_attrs}>{body}</body></html>"
    )


def write_epub(path: Path, files: dict[str, str | bytes], opf_path: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER.format(opf=opf_path))
        for name, data in files.items():
            z.writestr(name, data)
    return path


FONT = bytes(range(256)) * 8


def png() -> bytes:
    """A real 1×1 PNG: Qt keeps an anchor in front of an image only if it can load it."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")
UID = "urn:uuid:12345678-1234-1234-1234-123456789abc"


def _obfuscate(data: bytes) -> bytes:
    key = hashlib.sha1(UID.encode()).digest()
    return bytes(b ^ key[i % 20] for i, b in enumerate(data[:1040])) + data[1040:]


def make_epub3(tmp_path: Path, name: str = "night.epub") -> Path:
    css = (
        "@charset 'utf-8'; body { text-align: center; } .italic { font-style: italic } "
        ".smallcaps { font-variant: small-caps } .center { text-align: center } "
        "@font-face { font-family: X; src: url(../fonts/serif.otf) } h1 { font-size: 2em } "
        "/* comment */ epub|type { color: red }"
    )
    head = '<link rel="stylesheet" type="text/css" href="../css/book.css"/>'
    chapter_one = xhtml(
        """<section epub:type="chapter" id="ch1">
<h1>Chapter One</h1>
<p class="first">It was a <em>dark</em> and <strong>stormy</strong>
   night.<a epub:type="noteref" href="#n1"><sup>1</sup></a></p>
<p>one<span>two</span> three <span class="italic">styled</span> <span class="smallcaps">Caps</span></p>
<h2 id="storm">The Storm</h2>
<blockquote><p>A quoted passage.</p></blockquote>
<p>Line one<br/>Line two<br/>Line three</p>
<hr/>
<p>* * *</p>
<p><img src="../images/map%20one.png" alt="map"/></p>
<p>Before <span id="inside">the</span> anchor.</p>
<aside epub:type="footnote" id="n1"><p>1. A footnote.</p></aside>
<pre>verse line
  indented line</pre>
<div>Loose text in a div<p>and a paragraph</p>tail text</div>
</section>""",
        head,
        ' epub:type="bodymatter"',
    )
    nav = xhtml(
        """<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>
<li><a href="text/chapter%20one.xhtml">Chapter One</a><ol><li><a href="text/chapter%20one.xhtml#storm">The Storm</a></li></ol></li>
<li><a href="text/chapter2.xhtml#c2">Chapter Two</a></li>
<li><a href="text/copyright.xhtml">Copyright</a></li>
</ol></nav>
<nav epub:type="landmarks"><ol><li><a epub:type="bodymatter" href="text/chapter%20one.xhtml">Start</a></li></ol></nav>"""
    )
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">{UID}</dc:identifier>
<dc:title>The Long Night</dc:title>
<dc:creator id="a1">Ana Lima</dc:creator><meta refines="#a1" property="role">aut</meta>
<dc:creator id="a2">Rui Costa</dc:creator><meta refines="#a2" property="role">trl</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="css" href="css/book.css" media-type="text/css"/>
<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>
<item id="map" href="images/map one.png" media-type="image/png"/>
<item id="font" href="fonts/serif.otf" media-type="font/otf"/>
<item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>
<item id="title" href="text/title.xhtml" media-type="application/xhtml+xml"/>
<item id="copyright" href="text/copyright.xhtml" media-type="application/xhtml+xml"/>
<item id="tocpage" href="text/toc-page.xhtml" media-type="application/xhtml+xml"/>
<item id="ch1" href="text/chapter%20one.xhtml" media-type="application/xhtml+xml"/>
<item id="notes" href="text/notes.xhtml" media-type="application/xhtml+xml"/>
<item id="ch2" href="text/chapter2.xhtml" media-type="application/xhtml+xml"/>
<item id="ack" href="text/ack.xhtml" media-type="application/xhtml+xml"/>
<item id="index" href="text/index.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="cover"/><itemref idref="title"/><itemref idref="copyright"/><itemref idref="tocpage"/>
<itemref idref="ch1"/><itemref idref="notes" linear="no"/><itemref idref="ch2"/><itemref idref="ack"/><itemref idref="index"/></spine>
</package>"""
    files = {
        "OEBPS/content.opf": opf,
        "OEBPS/nav.xhtml": nav,
        "OEBPS/css/book.css": css,
        "OEBPS/images/cover.jpg": png(),
        "OEBPS/images/map one.png": png(),
        "OEBPS/fonts/serif.otf": _obfuscate(FONT),
        "META-INF/encryption.xml": """<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
 xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>
<enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
<enc:CipherData><enc:CipherReference URI="OEBPS/fonts/serif.otf"/></enc:CipherData></enc:EncryptedData></encryption>""",
        "OEBPS/text/cover.xhtml": xhtml('<img src="../images/cover.jpg" alt="Cover"/>', "", ' epub:type="cover"'),
        "OEBPS/text/title.xhtml": xhtml(
            '<section epub:type="titlepage"><h1>The Long Night</h1><p>By <b>Ana Lima</b></p></section>', head
        ),
        "OEBPS/text/copyright.xhtml": xhtml(
            '<section epub:type="copyright-page"><p>Copyright © 2024 Ana Lima</p><p>All rights reserved.</p></section>'
        ),
        "OEBPS/text/toc-page.xhtml": xhtml(
            '<nav epub:type="toc"><h2>Contents</h2><ol><li><a href="chapter%20one.xhtml">Chapter One</a></li>'
            '<li><a href="chapter2.xhtml#c2">Chapter Two</a></li></ol></nav>'
        ),
        "OEBPS/text/chapter one.xhtml": chapter_one,
        "OEBPS/text/notes.xhtml": xhtml("<p>Not in the reading order.</p>"),
        "OEBPS/text/chapter2.xhtml": xhtml(
            '<h1 id="c2">Chapter Two</h1><p>Plain&nbsp;text&mdash;with entities and a <a href="chapter%20one.xhtml#storm">link</a>.</p>'
            '<p class="center">Centered line.</p><p>Out <a href="https://example.com/x">there</a>.</p>',
            head,
        ),
        "OEBPS/text/ack.xhtml": xhtml('<section epub:type="acknowledgments"><h1>Acknowledgments</h1><p>Thanks to all.</p></section>'),
        "OEBPS/text/index.xhtml": xhtml('<section epub:type="index"><h1>Index</h1><p>storm, 3</p></section>'),
    }
    return write_epub(tmp_path / name, files, "OEBPS/content.opf")


def make_epub2(tmp_path: Path) -> Path:
    css = ".calibre_bold { font-weight: bold } .calibre_it { font-style: italic }"
    head = '<link rel="stylesheet" type="text/css" href="style.css"/>'
    ncx = """<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>
<navPoint id="n1"><navLabel><text>Chapter I</text></navLabel><content src="ch1.html"/>
  <navPoint id="n2"><navLabel><text>A Section</text></navLabel><content src="ch1.html#sec"/></navPoint></navPoint>
<navPoint id="n3"><navLabel><text>Chapter II</text></navLabel><content src="ch2.html#part2"/></navPoint>
</navMap></ncx>"""
    opf = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:title>Old Tales</dc:title><dc:creator opf:role="aut">John Smith</dc:creator><dc:identifier id="id">x</dc:identifier>
<meta name="cover" content="cimg"/></metadata>
<manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="cimg" href="cover.jpeg" media-type="image/jpeg"/>
<item id="css" href="style.css" media-type="text/css"/>
<item id="cover" href="titlepage.xhtml" media-type="application/xhtml+xml"/>
<item id="contents" href="contents.html" media-type="application/xhtml+xml"/>
<item id="ch1" href="ch1.html" media-type="application/xhtml+xml"/>
<item id="ch2" href="ch2.html" media-type="application/xhtml+xml"/></manifest>
<spine toc="ncx"><itemref idref="cover"/><itemref idref="contents"/><itemref idref="ch1"/><itemref idref="ch2"/></spine>
<guide><reference type="cover" href="titlepage.xhtml" title="Cover"/>
<reference type="toc" href="contents.html#toc" title="Contents"/>
<reference type="text" href="ch1.html" title="Start"/></guide></package>"""
    files = {
        "content.opf": opf,
        "toc.ncx": ncx,
        "style.css": css,
        "cover.jpeg": png(),
        "titlepage.xhtml": xhtml(
            '<div><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<image xlink:href="cover.jpeg"/></svg></div>'
        ),
        "contents.html": xhtml('<p id="toc"><a href="ch1.html">Chapter I</a></p><p><a href="ch2.html#part2">Chapter II</a></p>'),
        "ch1.html": xhtml(
            '<p class="calibre_head">CHAPTER I</p><p class="calibre">It began &mdash; as tales do&nbsp;&mdash; at night,'
            ' <span class="calibre_it">long</span> ago.</p><p id="sec" class="calibre">A <span class="calibre_bold">second</span> part.</p>',
            head,
        ),
        "ch2.html": xhtml('<div><a id="part2"/></div><h2>CHAPTER II</h2><p>The end of it.</p>', head),
    }
    return write_epub(tmp_path / "tales.epub", files, "content.opf")


def aligns(block_text: str, doc_text: str) -> bool:
    """Does the rendered text from a block's anchor on spell the block's text? Whitespace
    is ignored and a hyphen the block doesn't have is skipped (a hard-wrapped TXT keeps
    its line-end hyphens in original.html: "independ-\nent"). This is how the reader view
    is meant to align a block's characters."""
    want = [c for c in block_text if not c.isspace()]
    k = 0
    for c in doc_text:
        if k == len(want):
            return True
        if c.isspace() or c in "\ufffc\u2028\u2029":
            continue
        if c == want[k]:
            k += 1
        elif c not in "-\u00ad":
            return False
    return k == len(want)


def qt_check(book) -> tuple[list[int], list[int]]:
    """Render original_html with Qt: (blocks whose anchor is missing, blocks whose text differs)."""
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setHtml(book.original_html)
    pos: dict[int, int] = {}
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            for name in fragment.charFormat().anchorNames():
                if name.startswith("ls-b"):
                    pos.setdefault(int(name[4:]), fragment.position())
            it += 1
        block = block.next()
    text = doc.toPlainText()
    order = sorted(pos.items(), key=lambda kv: kv[1])
    differ = []
    for k, (i, p) in enumerate(order):
        end = order[k + 1][1] if k + 1 < len(order) else len(text)
        if not aligns(book.blocks[i].text, text[p:end]):
            differ.append(i)
    return [i for i in range(len(book.blocks)) if i not in pos], differ


def by_text(book, text: str, kind: str | None = None):
    return next(b for b in book.blocks if b.text == text and (kind is None or b.kind == kind))


# ---------------------------------------------------------------------------
# EPUB3


def test_epub3_metadata_kinds_and_text(tmp_path):
    book = parse_epub(make_epub3(tmp_path), "b1")
    assert (book.title, book.author) == ("The Long Night", "Ana Lima")
    texts = [b.text for b in book.blocks]
    assert "Not in the reading order." not in texts  # linear="no"
    assert by_text(book, "The Storm").kind == "heading"
    assert by_text(book, "Chapter One", "heading").level < by_text(book, "The Storm").level
    assert by_text(book, "A quoted passage.").kind == "quote"
    assert by_text(book, "Line one\nLine two\nLine three").kind == "verse"
    assert by_text(book, "verse line\nindented line").kind == "verse"
    assert by_text(book, "1. A footnote.").kind == "note"
    assert by_text(book, "* * *").kind == "separator"
    assert any(b.kind == "separator" and b.text == "" for b in book.blocks)  # <hr/>
    assert [t for t in texts if t in ("Loose text in a div", "and a paragraph", "tail text")] == [
        "Loose text in a div", "and a paragraph", "tail text",
    ]
    # whitespace as a browser shows it: collapsed, and no space where the markup has none
    assert "It was a dark and stormy night.1" in texts
    assert "onetwo three styled Caps" in texts
    assert "Plain text—with entities and a link." in texts
    assert by_text(book, "Centered line.").align == "center"
    assert all(b.text == b.text.strip() and "  " not in b.text for b in book.blocks)


def test_epub3_inline_styles(tmp_path):
    book = parse_epub(make_epub3(tmp_path), "b1")
    first = by_text(book, "It was a dark and stormy night.1")
    assert set(first.styles) == {(9, 13, "i"), (18, 24, "b"), (31, 32, "sup")}
    second = by_text(book, "onetwo three styled Caps")
    assert (13, 19, "i") in second.styles and (20, 24, "sc") in second.styles


def test_epub3_images_fonts_and_cover(tmp_path):
    book = parse_epub(make_epub3(tmp_path), "b1")
    res = paths.book_dir("b1") / "res"
    images = [b for b in book.blocks if b.kind == "image"]
    assert [b.src for b in images] == ["cover.jpg", "map_one.png"]
    assert (res / "map_one.png").read_bytes() == png()
    assert images[0].role == "cover"
    assert book.fonts == ["serif.otf"]
    assert (res / "serif.otf").read_bytes() == FONT  # de-obfuscated


def test_epub3_anchors_and_toc(tmp_path):
    book = parse_epub(make_epub3(tmp_path), "b1")
    heading = by_text(book, "Chapter One", "heading")
    assert heading.anchors[:2] == ["text/chapter one.xhtml", "text/chapter one.xhtml#ch1"]
    assert "text/chapter one.xhtml#storm" in by_text(book, "The Storm").anchors
    assert "text/chapter one.xhtml#inside" in by_text(book, "Before the anchor.").anchors
    toc = [(e.title, book.blocks[e.block].text, e.level) for e in book.toc]
    assert toc == [("Chapter One", "Chapter One", 1), ("The Storm", "The Storm", 2), ("Chapter Two", "Chapter Two", 1)]


def test_epub3_roles_and_contents_links(tmp_path):
    book = parse_epub(make_epub3(tmp_path), "b1")
    roles = {b.text: b.role for b in book.blocks if b.text}
    assert roles["The Long Night"] == TITLE_ROLE
    assert roles["By Ana Lima"] == "frontmatter"
    assert roles["Copyright © 2024 Ana Lima"] == roles["All rights reserved."] == "copyright"
    assert roles["Contents"] == "contents"
    assert roles["Thanks to all."] == "acknowledgments"
    assert roles["storm, 3"] == roles["Index"] == "index"
    assert roles["It was a dark and stormy night.1"] == ""
    assert sum(b.role == TITLE_ROLE for b in book.blocks) == 1
    lines = [b for b in book.blocks if b.role == "contents" and b.kind != "heading"]
    assert [(b.text, book.blocks[b.link].text) for b in lines] == [("Chapter One", "Chapter One"), ("Chapter Two", "Chapter Two")]
    first_read = next(i for i, b in enumerate(book.blocks) if b.role not in SKIPPED_ROLES and b.role != TITLE_ROLE and b.text)
    assert book.blocks[first_read].text == "Chapter One"


def test_epub3_original_html(tmp_path, qapp):
    book = parse_epub(make_epub3(tmp_path), "b1")
    html = book.original_html
    missing, differ = qt_check(book)
    assert missing == [] and differ == []
    assert "text-align: center; }" not in html and "@font-face" not in html and "epub|type" not in html
    assert ".italic {font-style: italic}" in html
    res = paths.book_dir("b1") / "res"
    assert f'src="{(res / "map_one.png").as_uri()}"' in html
    storm = next(i for i, b in enumerate(book.blocks) if b.text == "The Storm")
    assert f'href="#ls-b{storm}"' in html
    assert 'href="https://example.com/x"' in html
    assert "<section" not in html and "epub:type" not in html


def test_epub_cache_and_load_book(tmp_path):
    path = make_epub3(tmp_path)
    book = load_book(path)
    assert book.source_kind == "epub" and book.toc
    assert (paths.book_dir(book.book_id) / "res" / "cover.jpg").exists()


# ---------------------------------------------------------------------------
# EPUB2


def test_epub2_ncx_guide_and_calibre_markup(tmp_path, qapp):
    book = parse_epub(make_epub2(tmp_path), "b2")
    assert (book.title, book.author) == ("Old Tales", "John Smith")
    first = by_text(book, "CHAPTER I", "heading")  # a "CHAPTER I" paragraph opening its file
    assert first.level == 1
    began = "It began — as tales do — at night, long ago."
    assert by_text(book, began).styles == [(began.index("long"), began.index("long") + 4, "i")]
    assert by_text(book, "A second part.").styles == [(2, 8, "b")]
    toc = [(e.title, book.blocks[e.block].text, e.level) for e in book.toc]
    assert toc == [("Chapter I", "CHAPTER I", 1), ("A Section", "A second part.", 2), ("Chapter II", "CHAPTER II", 1)]
    assert book.blocks[0].kind == "image" and book.blocks[0].role == "cover"  # svg <image>, guide "cover"
    contents = [b for b in book.blocks if b.role == "contents"]
    assert [book.blocks[b.link].text for b in contents] == ["CHAPTER I", "CHAPTER II"]
    assert qt_check(book) == ([], [])


def test_drm_and_damage(tmp_path):
    path = make_epub3(tmp_path)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr(
            "META-INF/rights.xml", "<rights/>"
        )
    with pytest.raises(BookImportError, match="DRM"):
        parse_epub(path, "x")
    drm = write_epub(
        tmp_path / "drm.epub",
        {
            "content.opf": "<package/>",
            "META-INF/encryption.xml": """<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
 xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
<enc:CipherData><enc:CipherReference URI="ch1.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>""",
        },
        "content.opf",
    )
    with pytest.raises(BookImportError, match="DRM"):
        parse_epub(drm, "x")
    empty = write_epub(tmp_path / "empty.epub", {"content.opf": "<package/>"}, "content.opf")
    with pytest.raises(BookImportError, match="damaged"):
        parse_epub(empty, "x")


# ---------------------------------------------------------------------------
# Running heads


def test_running_heads_glued_into_paragraphs_are_cut_from_blocks_and_html(tmp_path, qapp):
    filler = " ".join(["word"] * 140)
    pages = []
    for n in range(10, 20):
        head = f"{n} ARTFUL SENTENCES" if n % 2 == 0 else f"SHORT SENTENCES {n}"
        pages.append(f"<p>{filler} the walls {head} are white.</p>")
    opf = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata
 xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Artful Sentences: Syntax as Style</dc:title></metadata>
<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/></spine></package>"""
    body = "<h1>CHAPTER ONE</h1><h2>Short Sentences</h2><p>In 1984 George Orwell wrote it.</p>" + "".join(pages)
    path = write_epub(tmp_path / "artful.epub", {"content.opf": opf, "c.xhtml": xhtml(body)}, "content.opf")
    book = parse_epub(path, "b3")
    text = "\n".join(b.text for b in book.blocks)
    assert "ARTFUL SENTENCES" not in text and "SHORT SENTENCES 1" not in text
    assert text.count("the walls are white.") == 10
    assert "In 1984 George Orwell wrote it." in text and "CHAPTER ONE" in text
    assert "ARTFUL SENTENCES" not in book.original_html
    assert qt_check(book) == ([], [])


def make_scan_epub(tmp_path: Path, extra: dict[int, str] | None = None, name: str = "scan.epub") -> Path:
    """An Internet Archive-style EPUB: one file per scanned page, running heads, page numbers."""
    pages = {
        1: '<b>The text on this page is estimated to be only 12.00% accurate</b><p>a, —_— wht</p><img src="image_0001.jpeg"/>',
        2: "<p>Digitized by the Internet Archive in 2022 with funding from Kahle/Austin Foundation</p>",
        3: "<p>THE STORY and Other Tales by Ana Lima ALFRED A. KNOPF</p>",
        4: "<p>COPYRIGHT, 1923, BY ANA LIMA. Printed in the United States of America</p>",
        5: "<p>THE STORY It was the first page of the story and it went on and on until the gov[7]</p>",
        6: "<p>THE STORY ernment office closed its doors for the night, and the clerk went [8]</p>",
        7: "<p>THE STOKY home along the river. He said: “Good night.” [9]</p>",
        8: "<p>THE STORY The next morning was bright and cold, and he woke up early. [10]</p>",
        9: "<p>TlHE ST ORY when he came back the house was empty and dark. [11]</p>",
        **(extra or {}),
    }
    files = {"EPUB/image_0001.jpeg": png()}
    manifest, spine = [], []
    files["EPUB/notice.html"] = xhtml("<p>This book was produced in EPUB format by the Internet Archive.</p>")
    manifest.append('<item id="notice" href="notice.html" media-type="text/html"/>')
    spine.append('<itemref idref="notice"/>')
    for n, body in pages.items():
        files[f"EPUB/page_{n}.html"] = xhtml(body)
        manifest.append(f'<item id="p{n}" href="page_{n}.html" media-type="text/html"/>')
        spine.append(f'<itemref idref="p{n}"/>')
    files["EPUB/content.opf"] = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata/>'
        f"<manifest>{''.join(manifest)}</manifest><spine>{''.join(spine)}</spine></package>"
    )
    return write_epub(tmp_path / name, files, "EPUB/content.opf")


def test_internet_archive_scan(tmp_path, qapp):
    book = parse_epub(make_scan_epub(tmp_path), "b4")
    assert book.scanned
    assert (book.title, book.author) == ("The Story and Other Tales", "Ana Lima")
    texts = [b.text for b in book.blocks]
    assert not any("estimated to be only" in t for t in texts)
    assert "THE STORY" in texts and by_text(book, "THE STORY").kind == "heading"
    story = next(t for t in texts if t.startswith("It was the first page"))
    assert "until the government office closed its doors" in story  # "gov[7]" + "ernment"
    assert "the clerk went home along the river" in story
    assert "[" not in story and "STOKY" not in story and "ST ORY" not in story
    assert "The next morning was bright" in texts[texts.index(story) + 1]
    assert "when he came back the house was empty and dark." in texts[texts.index(story) + 1]
    roles = {b.text: b.role for b in book.blocks if b.text}
    assert roles["a, —_— wht"] == "boilerplate"
    assert roles["This book was produced in EPUB format by the Internet Archive."] == "boilerplate"
    assert roles["Digitized by the Internet Archive in 2022 with funding from Kahle/Austin Foundation"] == "boilerplate"
    assert roles["THE STORY and Other Tales by Ana Lima ALFRED A. KNOPF"] == TITLE_ROLE
    assert roles["COPYRIGHT, 1923, BY ANA LIMA. Printed in the United States of America"] == "copyright"
    assert [e.title for e in book.toc] == ["The Story"]
    assert "STOKY" not in book.original_html and "[8]" not in book.original_html
    assert qt_check(book) == ([], [])


# ---------------------------------------------------------------------------
# Real books (slow)


def _real(name: str) -> Path:
    path = EBOOKS / name
    if not path.is_file():
        pytest.skip(f"sample missing: {name}")
    return path


def _sound(book) -> None:
    assert all(b.text.strip() or b.kind in ("image", "separator") for b in book.blocks), "empty blocks"
    assert sum(b.role == TITLE_ROLE for b in book.blocks) <= 1
    assert all(0 <= e.block < len(book.blocks) for e in book.toc)
    assert [e.block for e in book.toc] == sorted(e.block for e in book.toc)
    for b in book.blocks:
        for s, e, st in b.styles:
            assert 0 <= s < e <= len(b.text), (b.text[:40], s, e, st)
    # no words run together by whitespace collapsing: "word.Next" or "wordNext"
    joined = sum(len(re.findall(r"[a-z]{3}[.,;:!?][A-Z][a-z]", b.text)) for b in book.blocks)
    assert joined <= len(book.blocks) // 200 + 2


@pytest.mark.slow
def test_real_anna_karenina(qapp):
    t = time.perf_counter()
    book = parse_epub(_real("leo-tolstoy_anna-karenina.epub"), "ak")
    elapsed = time.perf_counter() - t
    print(f"\nAnna Karenina: {len(book.blocks)} blocks, {len(book.toc)} TOC entries, {elapsed:.2f} s")
    assert elapsed < 10
    _sound(book)
    chapters = [e for e in book.toc if e.title.startswith("Chapter")]
    parts = [e for e in book.toc if e.title.startswith("Part")]
    assert len(parts) == 8 and len(chapters) == len(book.toc) - 8
    assert all(book.blocks[e.block].kind == "heading" and book.blocks[e.block].text == e.title for e in book.toc)
    assert book.blocks[book.toc[1].block + 1].text.startswith("Happy families are all alike")
    roles = Counter(b.role for b in book.blocks)
    assert roles["contents"] >= len(book.toc) and roles[TITLE_ROLE] == 1 and roles["cover"] == 1
    assert book.blocks[-1].role == "blurb" and "THE END" in [b.text for b in book.blocks]
    links = [b for b in book.blocks if b.link >= 0]
    assert len(links) >= 240 and all(book.blocks[b.link].text == b.text for b in links if b.text.startswith("Chapter"))
    assert any(st == "i" for b in book.blocks for _, _, st in b.styles)
    assert qt_check(book) == ([], [])


@pytest.mark.slow
@pytest.mark.parametrize("name, chapters", [("The Age of Innocence by Edith Wharton.epub", 34), ("Wuthering Heights by Emily Brontë.epub", 34)])
def test_real_standard_ebooks(qapp, name, chapters):
    t = time.perf_counter()
    book = parse_epub(_real(name), "se")
    elapsed = time.perf_counter() - t
    print(f"\n{name}: {len(book.blocks)} blocks, {len(book.toc)} TOC entries, {elapsed:.2f} s")
    _sound(book)
    roman = [e for e in book.toc if re.fullmatch(r"[IVXLC]+", e.title)]
    assert len(roman) == chapters
    title = next(b for b in book.blocks if b.role == TITLE_ROLE)
    assert title.text == book.title and title.kind == "heading"
    assert book.blocks[-1].role == "copyright"  # the Uncopyright page
    first_read = next(b for b in book.blocks if not b.role and b.text)
    assert first_read.kind == "heading"
    assert qt_check(book) == ([], [])


@pytest.mark.slow
def test_real_internet_archive_overcoat(qapp):
    t = time.perf_counter()
    book = parse_epub(_real("overcoatothersto0000niko.epub"), "ia")
    elapsed = time.perf_counter() - t
    print(f"\nOvercoat (IA scan): {len(book.blocks)} blocks, {len(book.toc)} TOC entries, {elapsed:.2f} s")
    _sound(book)
    titles = [e.title for e in book.toc]
    for story in ("The Overcoat", "The Carriage", "The Nevsky Prospect", "The Nose", "The Portrait"):
        assert story in titles
    text = "\n".join(b.text for b in book.blocks if not b.role)
    assert "estimated to be only" not in text
    assert "THE OVERCOAT mention" not in text and "A MADMAN’S DIARY" not in text.replace("a A MADMAN’S DIARY", "")
    assert not re.search(r"\[\d+\]", text)
    assert "in some government office, he found" in text  # "gov[7]" + page head + "ernment"
    for debris in ("BE OVERCOAT", "Marl Dee", "EVeSSCY", "TRANSLATOR (Se", "RIE IGr)"):  # OCR'd running heads
        assert debris not in text, debris
    sizes = sorted(len(b.text) for b in book.blocks if b.kind == "paragraph" and not b.role)
    assert sizes[len(sizes) // 2] < 1500 and sizes[-1] < 3000  # page-sized <p>s cut into paragraphs
    roles = Counter(b.role for b in book.blocks)
    assert roles["boilerplate"] >= 5 and roles["copyright"] == 1 and roles["contents"] == 1 and roles[TITLE_ROLE] == 1
    assert qt_check(book) == ([], [])


# ---------------------------------------------------------------------------
# Front matter as real e-books lay it out (from a sweep of the user's library)


def make_trade_epub(tmp_path: Path, name: str = "trade.epub") -> Path:
    """A publisher's EPUB2: praise page, title page with author and publisher, copyright,
    an epigraph, a contents page with no heading markup, and NCX labels naming them."""
    pages = {
        "praise": "<p>“A masterpiece of quiet menace.”</p><p>—The New York Times</p>"
                  "<p>“Unforgettable.”</p><p>—Joan Didion</p><p>“Brilliant and strange.”</p><p>—The Guardian</p>",
        "title": "<p class='t'>THE LONG NIGHT</p><p>A NOVEL</p><p>ANA LIMA</p><p>BLOOMSBURY</p>",
        "copy": "<p>Copyright © 2004 by Ana Lima</p><p>All rights reserved.</p><p>Printed in Brazil</p>",
        "epigraph": "<p>“You are all a lost generation.”</p><p>—Gertrude Stein</p>",
        "toc": "<p>CONTENTS</p><p><a href='ch1.html'>One</a></p><p><a href='ch2.html'>Two</a></p>",
        "ch1": "<p>One</p>" + "<p>It was night, and the river ran on past the house. " * 3 + "</p>",
        "ch2": "<p>Two</p><p>Morning came and the river was quiet again under the old bridge.</p>",
        "promo": "<p>Thank you for downloading this eBook.</p><p>Sign up for news and offers from our authors.</p>",
    }
    labels = [("Title Page", "title"), ("Copyright Page", "copy"), ("Table of Contents", "toc"),
              ("One", "ch1"), ("Two", "ch2")]
    ncx = "<?xml version='1.0'?><ncx xmlns='http://www.daisy.org/z3986/2005/ncx/'><navMap>" + "".join(
        f"<navPoint id='n{k}'><navLabel><text>{label}</text></navLabel><content src='{f}.html'/></navPoint>"
        for k, (label, f) in enumerate(labels)
    ) + "</navMap></ncx>"
    order = ["promo", "praise", "title", "copy", "epigraph", "toc", "ch1", "ch2"]
    opf = (
        "<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf' version='2.0'><metadata "
        "xmlns:dc='http://purl.org/dc/elements/1.1/'><dc:title>The Long Night (Picador Classics)</dc:title>"
        "<dc:creator>Lima, Ana</dc:creator></metadata><manifest><item id='ncx' href='toc.ncx' media-type='application/x-dtbncx+xml'/>"
        + "".join(f"<item id='{f}' href='{f}.html' media-type='application/xhtml+xml'/>" for f in order)
        + "</manifest><spine toc='ncx'>" + "".join(f"<itemref idref='{f}'/>" for f in order) + "</spine></package>"
    )
    files = {"content.opf": opf, "toc.ncx": ncx, **{f"{f}.html": xhtml(body) for f, body in pages.items()}}
    return write_epub(tmp_path / name, files, "content.opf")


def test_trade_epub_front_matter(tmp_path):
    book = parse_epub(make_trade_epub(tmp_path), "t1")
    assert (book.title, book.author) == ("The Long Night", "Ana Lima")  # "(Picador Classics)" is a series tag
    roles = {b.text: b.role for b in book.blocks}
    assert roles["Thank you for downloading this eBook."] == roles["Sign up for news and offers from our authors."] == "boilerplate"
    assert roles["“A masterpiece of quiet menace.”"] == roles["—The Guardian"] == "blurb"
    assert roles["THE LONG NIGHT"] == TITLE_ROLE
    assert roles["A NOVEL"] == roles["ANA LIMA"] == roles["BLOOMSBURY"] == "frontmatter"
    assert roles["Copyright © 2004 by Ana Lima"] == roles["Printed in Brazil"] == "copyright"
    assert roles["“You are all a lost generation.”"] == roles["—Gertrude Stein"] == ""  # an epigraph is read
    assert roles["CONTENTS"] == "contents" and by_text(book, "One", "paragraph").role == "contents"
    assert [e.title for e in book.toc] == ["One", "Two"]
    assert sum(b.role == "" for b in book.blocks) == 2 + 4 + 2  # epigraph, chapter one, chapter two


def test_unzipped_epub_folder(tmp_path):
    from livrosound.ingest import load_book

    zipped = make_epub2(tmp_path)
    folder = tmp_path / "unzipped" / "Tales.epub"
    with zipfile.ZipFile(zipped) as z:
        z.extractall(folder)
    book = load_book(folder)
    assert book.title == "Old Tales" and book.source_kind == "epub"
    assert [e.title for e in book.toc] == ["Chapter I", "A Section", "Chapter II"]
    assert load_book(folder).book_id == book.book_id


def make_calibre_parts_epub(tmp_path: Path) -> Path:
    """Maurice as calibre made it: a title page with a dedication, "PART" headings whose
    number sits in the next paragraph, chapters numbered "1", "2"… in bold paragraphs,
    and an NCX that lists only the parts."""
    # chapters a page's worth of words apart would be page numbers: these are 1,100 words long
    text = "<p class='t'>Once a term the whole school went for a walk, and everyone looked forward to it.</p>" * 70
    files = {
        "title.html": xhtml("<h1>Maurice</h1><p>by</p><p>E. M. FORSTER</p><p>A NOVEL</p><p>Dedicated to a Happier Year</p>"),
        "p1.html": xhtml("<h2 id='p1'><b>PART </b></h2><p> </p><p><i><b>One</b></i></p><p><b>1</b></p>" + text + "<p><b>2</b></p>" + text),
        "p2.html": xhtml("<h2><b>PART </b></h2><p><i><b>Two</b></i></p><p><b>3</b></p>" + text),
    }
    ncx = (
        "<?xml version='1.0'?><ncx xmlns='http://www.daisy.org/z3986/2005/ncx/'><navMap>"
        "<navPoint id='a'><navLabel><text>Maurice</text></navLabel><content src='title.html'/>"
        "<navPoint id='b'><navLabel><text>PART One</text></navLabel><content src='p1.html'/></navPoint>"
        "<navPoint id='c'><navLabel><text>PART Two</text></navLabel><content src='p2.html'/></navPoint>"
        "</navPoint></navMap></ncx>"
    )
    opf = (
        "<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf' version='2.0'><metadata "
        "xmlns:dc='http://purl.org/dc/elements/1.1/'><dc:title>Maurice (Penguin Classics)</dc:title>"
        "<dc:creator>E. M. Forster</dc:creator></metadata><manifest><item id='ncx' href='toc.ncx' media-type='application/x-dtbncx+xml'/>"
        + "".join(f"<item id='{f}' href='{f}.html' media-type='application/xhtml+xml'/>" for f in ("title", "p1", "p2"))
        + "</manifest><spine toc='ncx'><itemref idref='title'/><itemref idref='p1'/><itemref idref='p2'/></spine></package>"
    )
    return write_epub(tmp_path / "maurice.epub", {"content.opf": opf, "toc.ncx": ncx, **files}, "content.opf")


def test_parts_numbered_chapters_and_a_dedication(tmp_path, qapp):
    book = parse_epub(make_calibre_parts_epub(tmp_path), "m1")
    assert book.title == "Maurice"  # "(Penguin Classics)" is the series, not the title
    roles = {b.text: b.role for b in book.blocks}
    assert roles["Maurice"] == TITLE_ROLE and roles["E. M. FORSTER"] == "frontmatter"
    assert roles["Dedicated to a Happier Year"] == ""
    headings = [(b.text, b.level) for b in book.blocks if b.kind == "heading" and not b.role]
    assert headings == [("PART One", 1), ("1", 2), ("2", 2), ("PART Two", 1), ("3", 2)]
    assert "One" not in roles and "Two" not in roles
    assert [(e.title, e.level) for e in book.toc] == [("Part One", 1), ("1", 2), ("2", 2), ("Part Two", 1), ("3", 2)]
    assert qt_check(book) == ([], [])


def test_internet_archive_head_debris_and_page_sized_paragraphs(tmp_path, qapp):
    """OCR debris of a head is never read (user requirement), real page openings are kept,
    and the page-sized <p>s become paragraph-sized blocks that still align in Qt."""
    talk = " ".join(
        f"“Is it far to the river, sir?” asked the boy number {n}. The old man looked at the water for a long while "
        f"and said nothing at all, because he had said everything before."
        for n in range(1, 30)
    )
    extra = {
        10: "<p>at BE STORY: it up and put it in his pocket for later, and walked on. [12]</p>",
        11: "<p>HESSEN EVeSSCY eS P ROS PE Cor head and her curls were wet from the rain. [13]</p>",
        12: "<p>Marl Dee NG Veo id: PRO'S RIE IGr) the strains of music came from the house. [14]</p>",
        13: "<p>Akaky Akakyevitch crossed the kitchen and sat down by the stove. [15]</p>",
        14: "<p>CIA agents were not known in those days, of course, but the clerk feared them. [16]</p>",
        15: f"<p>THE STORY {talk} [17]</p>",
    }
    book = parse_epub(make_scan_epub(tmp_path, extra, "debris.epub"), "b5")
    text = "\n".join(b.text for b in book.blocks if not b.role)
    for debris in ("BE STORY", "EVeSSCY", "ROS PE Cor", "Marl Dee", "IGr)", "[1"):
        assert debris not in text, debris
    for kept in ("it up and put it in his pocket", "head and her curls", "the strains of music",
                 "Akaky Akakyevitch crossed the kitchen", "CIA agents were not known"):
        assert kept in text, kept
    sizes = [len(b.text) for b in book.blocks if b.kind == "paragraph" and not b.role]
    assert max(sizes) <= 1600 and sum(s >= 600 for s in sizes) >= 3
    talk_blocks = [b.text for b in book.blocks if "the old man looked" in b.text.lower()]
    assert len(talk_blocks) >= 3 and all(t.startswith("“Is it far") for t in talk_blocks[1:])  # cut at a turn of dialogue
    assert qt_check(book) == ([], [])
