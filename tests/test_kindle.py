"""AZW3 and MOBI to EPUB (nobookends_sync/kindle.py), against Calibre-made fixtures."""

import difflib
import io
import re
import struct
import zipfile
from pathlib import Path

import pytest

from nobookends_sync import kindle
from nobookends_sync.converted import cached_path, ensure_converted
from nobookends_sync.epub import load_epub

FIX = Path(__file__).parent / "fixtures"
KINDLE = FIX / "kindle"


def _tokens(source) -> list[str]:
    return load_epub(source).tokens


@pytest.mark.parametrize("name", ["rich.azw3", "rich.mobi", "rich-both.mobi", "alice.azw3", "alice.mobi"])
def test_converted_text_matches_the_source_epub(name):
    source = FIX / "alice.epub" if name.startswith("alice") else KINDLE / "rich.epub"
    want = _tokens(source)
    book = kindle.read_book((KINDLE / name).read_bytes())
    got = _tokens(io.BytesIO(kindle.write_epub(book)))
    # Every word of the source, in order. Calibre adds contents pages when it writes a
    # Kindle file (its own and the EPUB's nav), so the only extra words are contents labels.
    labels = {"table", "of", "contents"}

    def walk(points):
        for p in points:
            labels.update(load_words(p.label))
            walk(p.children)

    walk(book.nav)
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, want, got, autojunk=False).get_opcodes():
        assert op in ("equal", "insert"), (op, want[a1:a2], got[b1:b2])
        if op == "insert":
            assert set(got[b1:b2]) <= labels, got[b1:b2]


def load_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


@pytest.mark.parametrize("name", ["rich.azw3", "rich.mobi", "rich-both.mobi"])
def test_images_links_and_contents(name):
    book = kindle.read_book((KINDLE / name).read_bytes())
    assert book.title == "Pride and Prejudice (excerpt)"
    assert book.authors == ["Jane Austen"]
    assert len(book.resources) == 1 and book.resources[0].media_type.startswith("image/")
    labels = []

    def walk(points):
        for p in points:
            labels.append(p.label)
            walk(p.children)

    walk(book.nav)
    assert labels == ["Chapter One", "A Smaller Part", "Chapter Two"]
    epub = zipfile.ZipFile(io.BytesIO(kindle.write_epub(book)))
    names = set(epub.namelist())
    assert book.resources[0].name and f"OEBPS/{book.resources[0].name}" in names
    # the footnote link points at a file in the book, and the anchor it names exists there
    first = epub.read(f"OEBPS/{book.chapters[0].name}").decode()
    href = next(h for h in __import__("re").findall(r'href="([^"]+)"', first) if "#" in h and "css" not in h)
    target, anchor = href.split("#")
    assert f'id="{anchor}"' in epub.read(f"OEBPS/{target}").decode()


def test_kf8_keeps_nested_contents():
    book = kindle.read_book((KINDLE / "rich.azw3").read_bytes())
    assert [p.label for p in book.nav] == ["Chapter One", "Chapter Two"]
    assert [c.label for c in book.nav[0].children] == ["A Smaller Part"]


def test_epub_is_a_valid_package_and_the_same_every_time():
    data = (KINDLE / "rich.azw3").read_bytes()
    a, b = kindle.convert_to_epub(data), kindle.convert_to_epub(data)
    assert a == b
    z = zipfile.ZipFile(io.BytesIO(a))
    assert z.namelist()[0] == "mimetype" and z.read("mimetype") == b"application/epub+zip"
    assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    assert "OEBPS/nav.xhtml" in z.namelist() and "OEBPS/toc.ncx" in z.namelist()


def _with_encryption(data: bytes) -> bytes:
    (first,) = struct.unpack_from(">L", data, 78)
    out = bytearray(data)
    out[first + 12:first + 14] = b"\x00\x02"
    return bytes(out)


def test_drm_is_refused():
    with pytest.raises(kindle.DrmProtected):
        kindle.convert_to_epub(_with_encryption((KINDLE / "rich.azw3").read_bytes()))


def test_not_a_kindle_book():
    with pytest.raises(kindle.KindleError):
        kindle.convert_to_epub((KINDLE / "rich.epub").read_bytes())
    with pytest.raises(kindle.KindleError):
        kindle.convert_to_epub(b"BOOKMOBI")


def test_palmdoc():
    assert kindle.palmdoc_decompress(b"abcd") == b"abcd"
    # 1 to 8: that many raw bytes follow
    assert kindle.palmdoc_decompress(bytes([2]) + b"\x90\x91") == b"\x90\x91"
    # 0xC0 and up: a space and a character
    assert kindle.palmdoc_decompress(bytes([0xE1])) == b" a"
    # 0x80 to 0xBF: copy (pair & 7) + 3 bytes from (pair >> 3) & 0x7FF back
    pair = 0x8000 | (3 << 3) | 0
    assert kindle.palmdoc_decompress(b"abc" + struct.pack(">H", pair)) == b"abcabc"


def _huff_cdic(phrases: list[tuple[bytes, bool]]) -> tuple[bytes, bytes]:
    """A table with 8 bit codes: byte b decodes to phrase 255 - b."""
    cache = b"".join(struct.pack(">L", (255 << 8) | 0x80 | 8) for _ in range(256))
    base = b"\x00" * (64 * 4)
    huff = b"HUFF" + struct.pack(">LLL", 24, 24, 24 + len(cache)) + b"\x00" * 8 + cache + base
    offsets, body = [], b""
    table_len = 2 * len(phrases)
    for data, done in phrases:
        offsets.append(table_len + len(body))
        body += struct.pack(">H", len(data) | (0x8000 if done else 0)) + data
    cdic = b"CDIC" + struct.pack(">LLL", 16, len(phrases), 8) + b"".join(struct.pack(">H", o) for o in offsets) + body
    return huff, cdic


def test_huff_cdic():
    phrases = [(bytes([c]), True) for c in b"helo w"]
    phrases += [(b"", True)] * (256 - len(phrases))
    # phrase 6 is itself compressed: "lo" = phrases 2 and 3
    phrases[6] = (bytes([255 - 2, 255 - 3]), False)
    huff, cdic = _huff_cdic(phrases)
    reader = kindle.HuffCdic(huff, [cdic])
    encode = bytes(255 - i for i in [0, 1, 6, 4, 5])  # h e (lo) ' ' w
    assert reader.decompress(encode) == b"helo w"


def test_trailing_entries():
    # one multibyte overlap byte, then a 2 byte trailing entry (its size, 2, stored last)
    rec = b"text" + b"\x00" + b"\x00\x82"
    assert kindle.strip_trailing(rec, 0b11) == b"text"
    assert kindle.strip_trailing(b"text\x00", 0b1) == b"text"


def test_conversion_cache(tmp_path):
    data = (KINDLE / "rich.azw3").read_bytes()
    eb = {"ino": "9", "size": len(data), "mtime": 1, "format": "azw3"}
    calls = []

    def fetch():
        calls.append(1)
        return data

    p1 = ensure_converted(tmp_path, "ebook-1", eb, fetch)
    p2 = ensure_converted(tmp_path, "ebook-1", eb, fetch)
    assert p1 == p2 and calls == [1] and p1.exists()
    # the file changed: a new copy, and the old one is gone
    eb2 = dict(eb, mtime=2)
    p3 = ensure_converted(tmp_path, "ebook-1", eb2, fetch)
    assert p3 != p1 and p3.exists() and not p1.exists()
    assert p3 == cached_path(tmp_path, "ebook-1", eb2)
