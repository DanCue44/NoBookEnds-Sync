"""EPUB parsing with a mapping from text offsets back to EPUB CFIs.

For every spine item we build `text`: the exact DOM `body.textContent` of the
XHTML document (all descendant text nodes of <body> in document order, raw
whitespace included). Each text node ("piece") remembers its CFI location:
the element step path from the root <html> element, the CFI text-chunk index
(odd number) and the UTF-16 offset of the piece inside that chunk. That gives
two equivalent ways to point at a character:

* an EPUB CFI, e.g. ``epubcfi(/6/14[id1134]!/4/10/3:12)``
* ``href`` + a UTF-16 offset into ``body.textContent`` of that spine item

Offsets that leave this module towards clients are UTF-16 code units, which is
what JavaScript strings, the DOM and epub.js count. Internally we use Python
code point offsets and convert at the edge.
"""

from __future__ import annotations

import bisect
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote

from lxml import etree, html as lxml_html

from .textnorm import Utf16Index, tokenize, utf16_len

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "br", "caption", "dd",
    "div", "dl", "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "img", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
SKIP_TAGS = {"script", "style", "head", "title", "rt", "rp"}

ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc", "inc",
    "ltd", "co", "corp", "no", "vol", "fig", "gen", "gov", "sen", "rep", "rev",
    "capt", "col", "lt", "sgt", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec", "u.s", "e.g", "i.e", "a.m", "p.m",
}

# End of sentence: terminal punctuation, optional closing quotes/brackets, then
# whitespace. The next sentence must start with something sentence-like.
_EOS_RE = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s+[\"'“‘(\[]?[A-Z0-9À-ɏ])")
_WORD_BEFORE_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")


def _local(tag) -> str:
    try:
        return etree.QName(tag).localname.lower()
    except (ValueError, TypeError):
        return str(tag).lower()


def _cfi_escape(s: str) -> str:
    return re.sub(r"([\^\[\](),;=])", r"^\1", s)


@dataclass
class Piece:
    start: int          # code point offset into SpineDoc.text
    text: str
    path: str           # CFI element path from the root element, e.g. "/4/10[p5]"
    chunk: int          # odd CFI text-chunk index within the parent element
    chunk_off: int      # UTF-16 offset of this piece within its chunk
    skip: bool          # inside script/style: counted for offsets, never aligned


@dataclass
class SpineDoc:
    index: int          # position in the spine (0-based)
    idref: str
    href: str           # manifest href, relative to the OPF, as written
    spine_path: str     # CFI path to the itemref, e.g. "/6/14[id1134]"
    text: str = ""
    pieces: list[Piece] = field(default_factory=list)
    breaks: list[int] = field(default_factory=list)
    _starts: list[int] = field(default_factory=list)
    _u16: Utf16Index | None = None

    def finalize(self):
        self._starts = [p.start for p in self.pieces]
        self.breaks = sorted(set(self.breaks))
        self._u16 = Utf16Index(self.text)

    def utf16(self, pos: int) -> int:
        return self._u16(pos)

    def cfi(self, pos: int, end: bool = False) -> str:
        """CFI of the character at code point `pos`, or of the point just after
        character pos-1 when end=True (exclusive range end)."""
        target = pos - 1 if end else pos
        i = bisect.bisect_right(self._starts, target) - 1
        i = max(0, min(i, len(self.pieces) - 1))
        p = self.pieces[i]
        rel = min(max(target - p.start, 0), len(p.text))
        off = p.chunk_off + utf16_len(p.text[:rel]) + (utf16_len(p.text[rel:rel + 1]) if end else 0)
        return f"epubcfi({self.spine_path}!{p.path}/{p.chunk}:{off})"


@dataclass
class Sentence:
    doc: int
    start: int          # code point offsets into the doc text
    end: int
    tok_first: int
    tok_last: int       # exclusive


@dataclass
class Book:
    docs: list[SpineDoc]
    sentences: list[Sentence]
    tokens: list[str]               # normalized
    tok_sent: list[int]
    tok_start: list[int]            # code point offsets in the doc text
    tok_end: list[int]


def _parse_xml(data: bytes):
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        root = None
    if root is None or _local(root.tag) != "html":
        root = lxml_html.fromstring(data)
    return root


def _element_children(el):
    return [c for c in el if isinstance(c.tag, str)]


def _step(el, k: int) -> str:
    eid = el.get("id")
    return f"/{2 * k}" + (f"[{_cfi_escape(eid)}]" if eid else "")


class _DocBuilder:
    def __init__(self, doc: SpineDoc):
        self.doc = doc
        self.parts: list[str] = []
        self.pos = 0

    def emit(self, text: str, path: str, chunk: int, chunk_off: int, skip: bool):
        self.doc.pieces.append(Piece(self.pos, text, path, chunk, chunk_off, skip))
        self.parts.append(text)
        self.pos += len(text)

    def walk(self, el, path: str, skip: bool):
        name = _local(el.tag)
        skip = skip or name in SKIP_TAGS
        block = name in BLOCK_TAGS
        if block:
            self.doc.breaks.append(self.pos)
        chunk, chunk_off = 1, 0
        if el.text:
            self.emit(el.text, path, chunk, chunk_off, skip)
            chunk_off += utf16_len(el.text)
        k = 0
        for child in el:
            if isinstance(child.tag, str):
                k += 1
                self.walk(child, path + _step(child, k), skip)
                chunk, chunk_off = 2 * k + 1, 0
            if child.tail:
                self.emit(child.tail, path, chunk, chunk_off, skip)
                chunk_off += utf16_len(child.tail)
        if block:
            self.doc.breaks.append(self.pos)


def _build_doc(doc: SpineDoc, data: bytes):
    root = _parse_xml(data)
    body = None
    path = ""
    for k, child in enumerate(_element_children(root), start=1):
        if _local(child.tag) == "body":
            body = child
            path = _step(child, k)
            break
    if body is None:
        return
    b = _DocBuilder(doc)
    b.walk(body, path, False)
    doc.text = "".join(b.parts)


def _split_sentences(text: str, breaks: list[int]) -> list[tuple[int, int]]:
    bounds = [0] + [b for b in breaks if 0 < b < len(text)] + [len(text)]
    out = []
    for a, z in zip(bounds, bounds[1:]):
        if a >= z:
            continue
        block = text[a:z]
        cuts = [0]
        for m in _EOS_RE.finditer(block):
            if m.group(0).startswith("."):
                w = _WORD_BEFORE_RE.search(block[: m.start()])
                if w:
                    word = w.group(1).lower().strip(".")
                    if word in ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
                        continue
            cuts.append(m.end())
        cuts.append(len(block))
        for s, e in zip(cuts, cuts[1:]):
            seg = block[s:e]
            ls = len(seg) - len(seg.lstrip())
            rs = len(seg.rstrip())
            if rs > ls:
                out.append((a + s + ls, a + s + rs))
    return out


def load_epub(source) -> Book:
    """Load an EPUB from a path, bytes or file object."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    with zipfile.ZipFile(source) as z:
        names = set(z.namelist())
        container = etree.fromstring(z.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile").get("full-path")
        opf_dir = posixpath.dirname(rootfile)
        opf = etree.fromstring(z.read(rootfile), etree.XMLParser(recover=True))

        manifest = {}
        for item in opf.iterfind(".//{*}manifest/{*}item"):
            manifest[item.get("id")] = item
        pkg_children = _element_children(opf)
        spine_el = opf.find("{*}spine")
        spine_step = 2 * (pkg_children.index(spine_el) + 1)

        docs: list[SpineDoc] = []
        for i, ref in enumerate(_element_children(spine_el)):
            idref = ref.get("idref")
            item = manifest.get(idref)
            if item is None:
                continue
            mt = (item.get("media-type") or "").lower()
            if "html" not in mt:
                continue
            if "nav" in (item.get("properties") or "").split():
                continue
            href = item.get("href")
            zpath = posixpath.normpath(posixpath.join(opf_dir, unquote(href)))
            if zpath not in names:
                continue
            doc = SpineDoc(
                index=i,
                idref=idref,
                href=href,
                spine_path=f"/{spine_step}/{2 * (i + 1)}[{_cfi_escape(idref)}]",
            )
            _build_doc(doc, z.read(zpath))
            doc.finalize()
            if doc.text.strip():
                docs.append(doc)

    sentences: list[Sentence] = []
    tokens, tok_sent, tok_start, tok_end = [], [], [], []
    for di, doc in enumerate(docs):
        clean = list(doc.text)
        for p in doc.pieces:
            if p.skip:
                clean[p.start:p.start + len(p.text)] = " " * len(p.text)
        clean_text = "".join(clean)
        for s, e in _split_sentences(clean_text, doc.breaks):
            toks = tokenize(clean_text[s:e])
            if not toks:
                continue
            first = len(tokens)
            si = len(sentences)
            for n, a, b in toks:
                tokens.append(n)
                tok_sent.append(si)
                tok_start.append(s + a)
                tok_end.append(s + b)
            sentences.append(Sentence(di, s, e, first, len(tokens)))
    return Book(docs, sentences, tokens, tok_sent, tok_start, tok_end)


def sentence_text(doc: SpineDoc, s: Sentence) -> str:
    """Sentence text as served: the raw textContent slice with each whitespace
    character replaced by a plain space (length preserving, so offsets still
    line up with body.textContent)."""
    return re.sub(r"\s", " ", doc.text[s.start:s.end])
