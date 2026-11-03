"""EPUB offset and CFI mapping, checked against an independent CFI resolver."""

import posixpath
import re
import zipfile
from urllib.parse import unquote

from lxml import etree

from nobookends_sync.epub import load_epub, sentence_text


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _u16_slice(s: str, start: int, end: int | None = None) -> str:
    b = s.encode("utf-16-le")
    return b[start * 2: None if end is None else end * 2].decode("utf-16-le")


class Resolver:
    """Minimal EPUB CFI resolver written separately from the production code."""

    def __init__(self, path):
        self.z = zipfile.ZipFile(path)
        container = etree.fromstring(self.z.read("META-INF/container.xml"))
        self.opf_path = container.find(".//{*}rootfile").get("full-path")
        self.opf = etree.fromstring(self.z.read(self.opf_path))

    @staticmethod
    def _elements(el):
        return [c for c in el if isinstance(c.tag, str)]

    def _chunk_text(self, el, idx):
        """Text of CFI text chunk `idx` (odd) of element el."""
        k = (idx - 1) // 2  # number of element children before the chunk
        parts = []
        if k == 0 and el.text:
            parts.append(el.text)
        seen = 0
        for c in el:
            if isinstance(c.tag, str):
                seen += 1
                if seen > k:
                    break
            if seen == k and c.tail:
                parts.append(c.tail)
        return "".join(parts)

    def resolve(self, cfi):
        m = re.fullmatch(r"epubcfi\((.*)\)", cfi)
        pkg_path, doc_path = m.group(1).split("!")
        steps = re.findall(r"/(\d+)(?:\[([^\]]*)\])?", pkg_path)
        spine = self._elements(self.opf)[int(steps[0][0]) // 2 - 1]
        itemref = self._elements(spine)[int(steps[1][0]) // 2 - 1]
        assert steps[1][1] in ("", itemref.get("idref"))
        item = self.opf.find(f".//{{*}}item[@id='{itemref.get('idref')}']")
        zpath = posixpath.normpath(posixpath.join(posixpath.dirname(self.opf_path), unquote(item.get("href"))))
        root = etree.fromstring(self.z.read(zpath))
        *el_steps, last = doc_path.split("/")[1:]
        el = root
        for st in el_steps:
            n = int(re.match(r"\d+", st).group(0))
            assert n % 2 == 0
            el = self._elements(el)[n // 2 - 1]
            if "[" in st:
                assert el.get("id") == st[st.index("[") + 1: -1]
        idx, off = last.split(":")
        return self._chunk_text(el, int(idx)), int(off)


def test_spine_order_and_nav_skipped(book):
    assert [d.href for d in book.docs] == ["title.xhtml", "chapter1.xhtml", "text/chapter%202.xhtml"]
    assert [d.spine_path for d in book.docs] == ["/6/4[title]", "/6/6[ch1]", "/6/8[ch2]"]


def test_text_matches_dom_text_content(epub_path, book):
    with zipfile.ZipFile(epub_path) as z:
        for doc in book.docs:
            root = etree.fromstring(z.read("OEBPS/" + unquote(doc.href)))
            body = root.find("{*}body")
            # textContent: all descendant text nodes, comments excluded
            expected = "".join(t for t in body.xpath(".//text()"))
            assert doc.text == expected


def test_script_text_is_not_aligned(book):
    assert "ignored" not in book.tokens


def test_sentence_cfis_resolve_to_sentence_text(epub_path, book):
    r = Resolver(epub_path)
    for s in book.sentences:
        doc = book.docs[s.doc]
        text = sentence_text(doc, s)
        chunk, off = r.resolve(doc.cfi(s.start))
        assert _u16_slice(chunk, off, off + 1) == text[0]
        chunk, off = r.resolve(doc.cfi(s.end, end=True))
        assert _u16_slice(chunk, off - 1, off) == text[-1]


def test_text_offsets_are_utf16(book):
    doc = book.docs[1]
    sent = next(s for s in book.sentences if "\U0001F407" in doc.text[s.start:s.end] and s.doc == 1)
    t0 = doc.utf16(sent.start)
    t1 = doc.utf16(sent.end)
    assert _u16_slice(doc.text, t0, t1) == doc.text[sent.start:sent.end]
    # the word after the emoji is shifted by one extra UTF-16 unit
    j = next(j for j in range(sent.tok_first, sent.tok_last) if book.tokens[j] == "mister")
    assert doc.utf16(book.tok_start[j]) == book.tok_start[j] + 1
    assert _u16_slice(doc.text, doc.utf16(book.tok_start[j]), doc.utf16(book.tok_end[j])) == "Mr"


def test_comment_split_text_node_uses_one_chunk(epub_path, book):
    r = Resolver(epub_path)
    doc = book.docs[1]
    pos = doc.text.index("considering how")
    chunk, off = r.resolve(doc.cfi(pos))
    assert _u16_slice(chunk, off).startswith("considering how")


def test_inline_markup_cfi(epub_path, book):
    r = Resolver(epub_path)
    doc = book.docs[1]
    pos = doc.text.index("White Rabbit")
    cfi = doc.cfi(pos)
    assert cfi.endswith("/6/2/1:0)")  # first text node inside the <em>
    chunk, off = r.resolve(cfi)
    assert chunk == "White Rabbit"


def test_abbreviations_do_not_split(book):
    texts = [sentence_text(book.docs[s.doc], s) for s in book.sentences]
    assert any("Mr. Rabbit was nowhere to be seen." in t for t in texts)
    assert "Oh dear!" in texts  # but real sentence ends do


def test_headings_split_on_br(book):
    texts = [sentence_text(book.docs[s.doc], s) for s in book.sentences]
    assert "CHAPTER I." in texts and "Down the Rabbit-Hole" in texts


def test_load_from_bytes(epub_path):
    b = load_epub(epub_path.read_bytes())
    assert len(b.tokens) > 300
