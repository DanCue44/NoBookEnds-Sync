"""Kindle ebooks (AZW3 / KF8 and MOBI) to EPUB, in pure Python.

Listen & Read aligns narration to EPUB text, so a book whose only ebook is an
AZW3 or MOBI file is converted here first. The EPUB is built the same way
every time from the same file (fixed zip dates, stable names), so an alignment
made against it keeps matching the copy the app is sent later.

Handles KF8 (AZW3, and the KF8 half of a combined MOBI) and the older
Mobipocket format, uncompressed, PalmDoc and HUFF/CDIC compressed. Files with
DRM cannot be converted and raise DrmProtected.

Format notes follow the MobileRead wiki pages on the MOBI, PalmDOC and KF8
formats.
"""

from __future__ import annotations

import hashlib
import html
import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field

from lxml import etree
from lxml import html as lxml_html

# Bump when the EPUB this module builds changes: stored alignments for
# converted books are made again (see abs_client.file_signature).
CONVERTER_VERSION = 1

KINDLE_FORMATS = ("azw3", "azw", "mobi", "kf8", "prc")

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
NO_LINK = 0xFFFFFFFF


class KindleError(Exception):
    """The file is not a Kindle book this module can read."""


class DrmProtected(KindleError):
    """The file is encrypted (bought from a store with DRM)."""


# ------------------------------------------------------------------ PalmDB


class PalmDB:
    def __init__(self, data: bytes):
        if len(data) < 78:
            raise KindleError("file is too short to be a Kindle book")
        kind = data[60:68]
        if kind not in (b"BOOKMOBI", b"TEXtREAd"):
            raise KindleError("not a Kindle book (no BOOKMOBI header)")
        self.data = data
        (count,) = struct.unpack_from(">H", data, 76)
        offsets = [struct.unpack_from(">L", data, 78 + 8 * i)[0] for i in range(count)]
        offsets.append(len(data))
        self.records = [(offsets[i], offsets[i + 1]) for i in range(count)]

    def __len__(self):
        return len(self.records)

    def record(self, i: int) -> bytes:
        if i < 0 or i >= len(self.records):
            raise KindleError(f"record {i} is missing")
        a, b = self.records[i]
        return self.data[a:b]


def _u32(b: bytes, off: int) -> int:
    if off + 4 > len(b):
        return NO_LINK
    return struct.unpack_from(">L", b, off)[0]


@dataclass
class Header:
    """Record 0 of a book (MOBI 6 or KF8): PalmDOC + MOBI header + EXTH."""

    compression: int
    text_length: int
    text_records: int
    encryption: int
    mobi_length: int
    version: int
    encoding: str
    title: str
    first_resource: int
    huff_record: int
    huff_count: int
    trailing_flags: int
    ncx_index: int
    fdst_index: int
    frag_index: int
    skel_index: int
    exth: dict[int, list[bytes]] = field(default_factory=dict)

    @property
    def kf8(self) -> bool:
        return self.version >= 8


def _read_header(rec0: bytes) -> Header:
    compression, _, text_length, text_records, _, encryption = struct.unpack_from(">HHLHHH", rec0, 0)
    m = rec0[16:]
    if m[:4] != b"MOBI":
        if compression in (1, 2):  # plain PalmDoc
            return Header(compression, text_length, text_records, encryption, 0, 0, "cp1252", "",
                          NO_LINK, 0, 0, 0, NO_LINK, NO_LINK, NO_LINK, NO_LINK)
        raise KindleError("record 0 has no MOBI header")
    mobi_length = _u32(m, 4)
    codepage = _u32(m, 12)
    version = _u32(m, 20)
    title_off, title_len = _u32(m, 68), _u32(m, 72)
    encoding = "utf-8" if codepage == 65001 else "cp1252"
    exth: dict[int, list[bytes]] = {}
    if _u32(m, 112) & 0x40:
        e = 16 + mobi_length
        if rec0[e:e + 4] == b"EXTH":
            count = _u32(rec0, e + 8)
            p = e + 12
            for _ in range(count):
                if p + 8 > len(rec0):
                    break
                kind, size = struct.unpack_from(">LL", rec0, p)
                if size < 8:
                    break
                exth.setdefault(kind, []).append(rec0[p + 8:p + size])
                p += size
    title = rec0[title_off:title_off + title_len].decode(encoding, "replace") if title_len < 4096 else ""
    trailing = _u32(m, 224) & 0xFFFF if mobi_length >= 0xE4 else 0
    return Header(
        compression=compression,
        text_length=text_length,
        text_records=text_records,
        encryption=encryption,
        mobi_length=mobi_length,
        version=version,
        encoding=encoding,
        title=title,
        first_resource=_u32(m, 92),
        huff_record=_u32(m, 96),
        huff_count=_u32(m, 100),
        trailing_flags=trailing,
        ncx_index=_u32(m, 228) if mobi_length >= 0xE8 else NO_LINK,
        fdst_index=_u32(m, 176) if version >= 8 else NO_LINK,
        frag_index=_u32(m, 232) if version >= 8 else NO_LINK,
        skel_index=_u32(m, 236) if version >= 8 else NO_LINK,
        exth=exth,
    )


# ------------------------------------------------------------------ text records


def _trailing_size(data: bytes) -> int:
    """A size stored backwards at the end of a record (7 bits a byte)."""
    value = 0
    for b in data[-4:]:
        if b & 0x80:
            value = 0
        value = (value << 7) | (b & 0x7F)
    return value


def strip_trailing(data: bytes, flags: int) -> bytes:
    for bit in range(15, 0, -1):
        if flags & (1 << bit):
            n = _trailing_size(data)
            if n <= 0 or n > len(data):
                break
            data = data[:-n]
    if flags & 1 and data:
        data = data[:-((data[-1] & 0x3) + 1)]
    return data


def palmdoc_decompress(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if 1 <= c <= 8:
            out += data[i:i + c]
            i += c
        elif c < 0x80:
            out.append(c)
        elif c >= 0xC0:
            out += b" "
            out.append(c ^ 0x80)
        else:
            if i >= n:
                break
            pair = (c << 8) | data[i]
            i += 1
            distance = (pair >> 3) & 0x7FF
            length = (pair & 7) + 3
            if distance == 0 or distance > len(out):
                continue
            start = len(out) - distance
            for k in range(length):
                out.append(out[start + k])
    return bytes(out)


class HuffCdic:
    """The Huffman + phrase dictionary compression some Kindle books use."""

    def __init__(self, huff: bytes, cdics: list[bytes]):
        if huff[:4] != b"HUFF":
            raise KindleError("bad HUFF record")
        cache_off, base_off = struct.unpack_from(">LL", huff, 8)
        self.cache = []
        for v in struct.unpack_from(">256L", huff, cache_off):
            length, terminal, top = v & 0x1F, bool(v & 0x80), v >> 8
            self.cache.append((length, terminal, ((top + 1) << (32 - length)) - 1 if length else 0))
        pairs = struct.unpack_from(">64L", huff, base_off)
        self.min_code = [0] * 33
        self.max_code = [0] * 33
        for length in range(1, 33):
            self.min_code[length] = pairs[2 * (length - 1)] << (32 - length)
            self.max_code[length] = ((pairs[2 * (length - 1) + 1] + 1) << (32 - length)) - 1
        self.phrases: list[tuple[bytes, bool]] = []
        for cdic in cdics:
            if cdic[:4] != b"CDIC":
                raise KindleError("bad CDIC record")
            total, bits = struct.unpack_from(">LL", cdic, 8)
            count = min(1 << bits, total - len(self.phrases))
            for k in range(count):
                (off,) = struct.unpack_from(">H", cdic, 16 + 2 * k)
                (blen,) = struct.unpack_from(">H", cdic, 16 + off)
                start = 18 + off
                self.phrases.append((cdic[start:start + (blen & 0x7FFF)], bool(blen & 0x8000)))

    def decompress(self, data: bytes, depth: int = 0) -> bytes:
        if depth > 32:
            raise KindleError("HUFF/CDIC phrases nest too deeply")
        bits_left = len(data) * 8
        padded = data + b"\x00" * 8
        pos, shift = 0, 32
        word = struct.unpack_from(">Q", padded, 0)[0]
        out = bytearray()
        while True:
            if shift <= 0:
                pos += 4
                word = struct.unpack_from(">Q", padded, pos)[0]
                shift += 32
            code = (word >> shift) & 0xFFFFFFFF
            length, terminal, top = self.cache[code >> 24]
            if not terminal:
                while length < 32 and code < self.min_code[length]:
                    length += 1
                top = self.max_code[length]
            if length == 0:
                raise KindleError("bad HUFF code")
            shift -= length
            bits_left -= length
            if bits_left < 0:
                break
            index = (top - code) >> (32 - length)
            if index >= len(self.phrases):
                raise KindleError("HUFF code out of range")
            phrase, done = self.phrases[index]
            if not done:
                phrase = self.decompress(phrase, depth + 1)
                self.phrases[index] = (phrase, True)
            out += phrase
        return bytes(out)


def read_text(db: PalmDB, h: Header, start: int) -> bytes:
    if h.encryption:
        raise DrmProtected("this book is protected with DRM")
    huff = None
    if h.compression == 17480:
        base = start + h.huff_record
        huff = HuffCdic(db.record(base), [db.record(base + k) for k in range(1, h.huff_count)])
    elif h.compression not in (1, 2):
        raise KindleError(f"unknown compression {h.compression}")
    out = bytearray()
    for i in range(1, h.text_records + 1):
        rec = strip_trailing(db.record(start + i), h.trailing_flags)
        if h.compression == 2:
            out += palmdoc_decompress(rec)
        elif huff is not None:
            out += huff.decompress(rec)
        else:
            out += rec
    return bytes(out[:h.text_length]) if h.text_length else bytes(out)


# ------------------------------------------------------------------ INDX


def _varlen(data: bytes, off: int) -> tuple[int, int]:
    """A forward variable width value: (bytes used, value)."""
    value, used = 0, 0
    while off + used < len(data):
        b = data[off + used]
        used += 1
        value = (value << 7) | (b & 0x7F)
        if b & 0x80:
            break
    return used, value


def _cncx(db: PalmDB, first: int, count: int) -> dict[int, str]:
    strings: dict[int, str] = {}
    for k in range(count):
        rec = db.record(first + k)
        off = 0
        while off < len(rec) and rec[off] != 0:
            used, length = _varlen(rec, off)
            strings[k * 0x10000 + off] = rec[off + used:off + used + length].decode("utf-8", "replace")
            off += used + length
    return strings


def read_index(db: PalmDB, index: int) -> tuple[list[tuple[str, dict[int, list[int]]]], dict[int, str]]:
    """Entries (name, {tag: values}) of an INDX index, and its CNCX strings."""
    if index == NO_LINK or index >= len(db):
        return [], {}
    head = db.record(index)
    if head[:4] != b"INDX":
        raise KindleError("bad INDX record")
    header_len = _u32(head, 4)
    record_count = _u32(head, 24)
    cncx_count = _u32(head, 52)
    tagx = header_len
    if head[tagx:tagx + 4] != b"TAGX":
        raise KindleError("INDX without TAGX")
    tagx_len, control_bytes = _u32(head, tagx + 4), _u32(head, tagx + 8)
    table = [tuple(head[tagx + p:tagx + p + 4]) for p in range(12, tagx_len, 4)]
    strings = _cncx(db, index + record_count + 1, cncx_count)
    entries = []
    for r in range(index + 1, index + 1 + record_count):
        rec = db.record(r)
        idxt, count = _u32(rec, 20), _u32(rec, 24)
        starts = [struct.unpack_from(">H", rec, idxt + 4 + 2 * j)[0] for j in range(count)] + [idxt]
        for j in range(count):
            start, end = starts[j], starts[j + 1]
            name_len = rec[start]
            name = rec[start + 1:start + 1 + name_len].decode("latin-1")
            entries.append((name, _tag_map(rec, start + 1 + name_len, end, control_bytes, table)))
    return entries, strings


def _tag_map(rec: bytes, start: int, end: int, control_bytes: int, table) -> dict[int, list[int]]:
    found = []
    control = 0
    data = start + control_bytes
    for tag, per_entry, mask, end_flag in table:
        if end_flag == 1:
            control += 1
            continue
        value = rec[start + control] & mask
        if not value:
            continue
        if value == mask and bin(mask).count("1") > 1:
            used, size = _varlen(rec, data)
            data += used
            found.append((tag, None, size, per_entry))
        else:
            while mask and not mask & 1:
                mask >>= 1
                value >>= 1
            found.append((tag, value, None, per_entry))
    out: dict[int, list[int]] = {}
    for tag, count, size, per_entry in found:
        values = []
        if count is not None:
            for _ in range(count * per_entry):
                used, v = _varlen(rec, data)
                data += used
                values.append(v)
        else:
            consumed = 0
            while consumed < size and data < end:
                used, v = _varlen(rec, data)
                data += used
                consumed += used
                values.append(v)
        out[tag] = values
    return out


# ------------------------------------------------------------------ resources


def _image_type(data: bytes) -> tuple[str, str] | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:2] == b"BM":
        return "bmp", "image/bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def _font(data: bytes) -> tuple[bytes, str, str] | None:
    """A FONT resource record: (font bytes, extension, media type)."""
    if data[:4] != b"FONT" or len(data) < 24:
        return None
    size, flags, offset, key_len, key_start = struct.unpack_from(">LLLLL", data, 4)
    body = bytearray(data[offset:])
    if flags & 0x2 and key_len:
        key = data[key_start:key_start + key_len]
        for i in range(min(1040, len(body))):
            body[i] ^= key[i % key_len]
    if flags & 0x1:
        try:
            body = bytearray(zlib.decompress(bytes(body)))
        except zlib.error:
            return None
    raw = bytes(body[:size] if size else body)
    if raw[:4] == b"OTTO":
        return raw, "otf", "font/otf"
    return raw, "ttf", "font/ttf"


@dataclass
class Resource:
    name: str
    media_type: str
    data: bytes


class Resources:
    """Images and fonts by their 1-based Kindle resource number."""

    def __init__(self, db: PalmDB, first: int, last: int):
        self.by_number: dict[int, Resource] = {}
        if first == NO_LINK:
            return
        for n, r in enumerate(range(first, min(last, len(db))), start=1):
            data = db.record(r)
            kind = _image_type(data)
            if kind:
                ext, media = kind
                self.by_number[n] = Resource(f"images/img{n:05d}.{ext}", media, data)
                continue
            font = _font(data)
            if font:
                raw, ext, media = font
                self.by_number[n] = Resource(f"fonts/font{n:05d}.{ext}", media, raw)

    def get(self, n: int) -> Resource | None:
        return self.by_number.get(n)


# ------------------------------------------------------------------ the book


@dataclass
class Chapter:
    name: str
    root: etree._Element  # an XHTML <html> element
    title: str = ""


@dataclass
class NavPoint:
    label: str
    href: str
    children: list[NavPoint] = field(default_factory=list)


@dataclass
class Book:
    title: str
    authors: list[str]
    language: str
    identifier: str
    chapters: list[Chapter]
    styles: dict[str, str]  # file name -> CSS
    resources: list[Resource]
    nav: list[NavPoint]
    cover: str | None = None
    description: str = ""
    publisher: str = ""


def _base32(s: str) -> int:
    return int(s, 32)


def _exth_text(h: Header, kind: int) -> list[str]:
    return [v.decode(h.encoding, "replace").strip() for v in h.exth.get(kind, []) if v.strip()]


def _parse_part(markup: bytes, encoding: str) -> etree._Element:
    """One (X)HTML document, made into well formed XHTML."""
    text = markup.decode(encoding, "replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.I)
    try:
        parser = etree.XMLParser(recover=False, resolve_entities=False, no_network=True, huge_tree=True)
        root = etree.fromstring(text.encode("utf-8"), parser)
        if etree.QName(root).localname != "html":
            raise etree.XMLSyntaxError("no html root", None, 0, 0)
    except etree.XMLSyntaxError:
        root = lxml_html.document_fromstring(text or "<html><body></body></html>")
    return _as_xhtml(root)


_DROP_TAGS = {"script", "mbp:pagebreak", "guide", "mbp:section", "mbp:frameset", "mbp:slave-frame"}
_UNWRAP_TAGS = {"mbp:nu", "mbp:section", "mbp:frameset", "font", "mbp:pagebreak"}
_VALID_ATTR = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")


def _as_xhtml(root: etree._Element) -> etree._Element:
    """Copy any parsed tree into the XHTML namespace, dropping what EPUB readers choke on."""

    def local(el):
        tag = el.tag if isinstance(el.tag, str) else ""
        return tag.split("}", 1)[-1].lower() if "}" in tag else tag.lower()

    def build(src, parent):
        name = local(src)
        if name in ("script", "guide"):
            if parent is not None and src.tail:
                _append_text(parent, src.tail)
            return None
        if name in _UNWRAP_TAGS or ":" in name or not re.match(r"^[a-z][a-z0-9]*$", name):
            if parent is None:
                return None
            if src.text:
                _append_text(parent, src.text)
            for child in src:
                if isinstance(child.tag, str):
                    build(child, parent)
                elif child.tail:
                    _append_text(parent, child.tail)
            if src.tail:
                _append_text(parent, src.tail)
            return None
        el = etree.SubElement(parent, f"{{{XHTML_NS}}}{name}") if parent is not None \
            else etree.Element(f"{{{XHTML_NS}}}{name}", nsmap={None: XHTML_NS})
        for k, v in src.attrib.items():
            key = k.split("}", 1)[-1]
            if not _VALID_ATTR.match(key) or key.lower().startswith("on") or ":" in key:
                continue
            el.set(key, v)
        el.text = src.text
        for child in src:
            if isinstance(child.tag, str):
                build(child, el)
            elif child.tail:  # comment or processing instruction: keep its tail text
                _append_text(el, child.tail)
        if parent is not None:
            el.tail = src.tail
        return el

    out = build(root, None)
    if out is None or etree.QName(out).localname != "html":
        out = etree.Element(f"{{{XHTML_NS}}}html", nsmap={None: XHTML_NS})
    if out.find(f"{{{XHTML_NS}}}head") is None:
        out.insert(0, etree.Element(f"{{{XHTML_NS}}}head"))
    if out.find(f"{{{XHTML_NS}}}body") is None:
        body = etree.SubElement(out, f"{{{XHTML_NS}}}body")
        for child in list(out):
            if etree.QName(child).localname not in ("head", "body"):
                body.append(child)
    head = out.find(f"{{{XHTML_NS}}}head")
    if head.find(f"{{{XHTML_NS}}}title") is None:
        etree.SubElement(head, f"{{{XHTML_NS}}}title").text = ""
    return out


def _append_text(el: etree._Element, text: str):
    if len(el):
        last = el[-1]
        last.tail = (last.tail or "") + text
    else:
        el.text = (el.text or "") + text


# ------------------------------------------------------------------ KF8


_POS_FID = re.compile(rb"kindle:pos:fid:([0-9A-V]{4}):off:([0-9A-V]{10})")
_EMBED = re.compile(rb"kindle:embed:([0-9A-V]{4})(?:\?mime=[^\"')\s]*)?")
_FLOW = re.compile(rb"kindle:flow:([0-9A-V]{4})(?:\?mime=[^\"')\s]*)?")
_ID_ATTR = re.compile(rb"""<[^>]*\s(?:id|name)\s*=\s*['"]([^'"]*)['"]""", re.I)
_AID_ATTR = re.compile(rb"""<[^>]*\said\s*=\s*['"]([^'"]+)['"]""")


def _kf8_book(db: PalmDB, h: Header, start: int, first_resource: int | None = None) -> Book:
    raw = read_text(db, h, start)
    # Flows: 0 is the text, the rest are CSS and SVG
    flows = [raw]
    if h.fdst_index != NO_LINK:
        fdst = db.record(start + h.fdst_index)
        if fdst[:4] == b"FDST":
            count = _u32(fdst, 8)
            flows = []
            for k in range(count):
                a, b = struct.unpack_from(">LL", fdst, 12 + 8 * k)
                flows.append(raw[a:b])
    text = flows[0]

    skels, _ = read_index(db, start + h.skel_index if h.skel_index != NO_LINK else NO_LINK)
    frags, frag_strings = read_index(db, start + h.frag_index if h.frag_index != NO_LINK else NO_LINK)
    frag_table = []  # [insert position, selector, file, sequence, start, length]
    for name, tags in frags:
        frag_table.append([int(name), frag_strings.get(tags.get(2, [0])[0], ""), tags.get(3, [0])[0],
                           tags.get(4, [0])[0], tags.get(6, [0, 0])[0], tags.get(6, [0, 0])[1]])

    # Assemble each file: its skeleton with its fragments put in
    parts: list[tuple[int, int, bytes]] = []  # (start, end, markup) in assembled positions
    if skels:
        frag_ptr = 0
        for _, tags in skels:
            frag_count = tags.get(1, [0])[0]
            skel_pos, skel_len = tags.get(6, [0, 0])[:2]
            base = skel_pos + skel_len
            skeleton = text[skel_pos:base]
            for _ in range(frag_count):
                if frag_ptr >= len(frag_table):
                    break
                insert, _sel, _file, _seq, _st, length = frag_table[frag_ptr]
                piece = text[base:base + length]
                at = insert - skel_pos
                head, tail = skeleton[:at], skeleton[at:]
                if tail.find(b">") < tail.find(b"<") or head.rfind(b">") < head.rfind(b"<"):
                    # A broken insert point: put the fragment after the tag it names
                    aid = re.search(rb"'([^']+)'", _sel.encode()) if _sel else None
                    if aid:
                        m = re.search(rb"<[^>]*\said=['\"]" + re.escape(aid.group(1)) + rb"['\"][^>]*>", skeleton)
                        if m:
                            at = m.end()
                            frag_table[frag_ptr][0] = at + skel_pos
                skeleton = skeleton[:at] + piece + skeleton[at:]
                base += length
                frag_ptr += 1
            parts.append((skel_pos, base, skeleton))
    else:
        parts.append((0, len(text), text))

    names = [f"part{n:04d}.xhtml" for n in range(len(parts))]
    end_res = len(db)
    # A combined MOBI keeps its images in the first (MOBI 6) half for both halves
    if first_resource is None:
        first_resource = start + h.first_resource if h.first_resource != NO_LINK else NO_LINK
    resources = Resources(db, first_resource, start if first_resource < start else end_res)
    linked: dict[tuple[int, bytes], str] = {}

    def part_at(pos: int) -> int | None:
        for n, (a, b, _) in enumerate(parts):
            if a <= pos < b:
                return n
        return None

    def target(fid: bytes, off: bytes) -> str:
        return resolve(_base32(fid.decode()), _base32(off.decode()))

    def resolve(row: int, offset: int) -> str:
        if row >= len(frag_table):
            return names[0]
        pos = frag_table[row][0] + offset
        n = part_at(pos)
        if n is None:
            return names[0]
        block = parts[n][2]
        local_pos = pos - parts[n][0]
        gt, lt = block.find(b">", local_pos), block.find(b"<", local_pos)
        if lt == local_pos or (gt != -1 and (lt == -1 or gt < lt)):
            local_pos = gt + 1
        before = block[:local_pos]
        for m in reversed(list(re.finditer(rb"<[^<>]*>", before))):
            tag = m.group(0)
            if tag.startswith(b"<body"):
                return names[n]
            if tag.startswith(b"<meta"):
                continue
            idm = _ID_ATTR.match(tag)
            if idm:
                return f"{names[n]}#{idm.group(1).decode('utf-8', 'replace')}"
            aidm = _AID_ATTR.match(tag)
            if aidm:
                linked[(n, aidm.group(1))] = f"aid-{aidm.group(1).decode('ascii', 'replace')}"
                return f"{names[n]}#aid-{aidm.group(1).decode('ascii', 'replace')}"
        return names[n]

    used_resources: dict[int, Resource] = {}

    def embed(num: bytes, prefix: str) -> str:
        n = _base32(num.decode())
        res = resources.get(n)
        if not res:
            return ""
        used_resources[n] = res
        return prefix + res.name

    styles: dict[str, str] = {}

    def flow(num: bytes, prefix: str) -> str:
        n = _base32(num.decode())
        if n >= len(flows):
            return ""
        name = f"styles/flow{n:04d}.css"
        if name not in styles:
            body = flows[n]
            if body.lstrip().startswith((b"<svg", b"<?xml")):
                name = f"images/flow{n:04d}.svg"
            styles[name] = _EMBED.sub(lambda m: embed(m.group(1), "../").encode(), body).decode(
                h.encoding, "replace")
        return prefix + name

    # Links first (they may name aids that must become ids)
    rewritten = []
    for n, (_, _, markup) in enumerate(parts):
        markup = _POS_FID.sub(lambda m: target(m.group(1), m.group(2)).encode(), markup)
        rewritten.append(markup)
    chapters = []
    for n, markup in enumerate(rewritten):
        markup = _EMBED.sub(lambda m: embed(m.group(1), "").encode(), markup)
        markup = _FLOW.sub(lambda m: flow(m.group(1), "").encode(), markup)
        markup = _keep_linked_aids(markup, {aid for (p, aid) in linked if p == n})
        chapters.append(Chapter(names[n], _parse_part(markup, h.encoding)))

    nav = _kf8_nav(db, h, start, resolve)
    cover = None
    if 201 in h.exth:
        res = resources.get(struct.unpack(">L", h.exth[201][0][:4])[0] + 1)
        if res:
            used_resources[-1] = res
            cover = res.name
    return _finish(h, chapters, styles, list(used_resources.values()), nav, cover)


def _keep_linked_aids(markup: bytes, aids: set[bytes]) -> bytes:
    """Kindle's aid attributes are not XHTML: keep the linked ones as ids."""

    def repl(m: re.Match) -> bytes:
        tag = m.group(0)
        am = re.search(rb"""\said\s*=\s*['"]([^'"]+)['"]""", tag)
        if not am:
            return tag
        if am.group(1) in aids and not re.search(rb"""\sid\s*=""", tag):
            return tag[:am.start()] + b' id="aid-' + am.group(1) + b'"' + tag[am.end():]
        return tag[:am.start()] + tag[am.end():]

    return re.sub(rb"<[^<>]*\said\s*=[^<>]*>", repl, markup)


def _kf8_nav(db: PalmDB, h: Header, start: int, resolve) -> list[NavPoint]:
    if h.ncx_index == NO_LINK:
        return []
    try:
        entries, strings = read_index(db, start + h.ncx_index)
    except KindleError:
        return []
    points: list[tuple[int, NavPoint, int, int]] = []  # (depth, point, parent, entry number)
    for number, (_, tags) in enumerate(entries):
        label = strings.get(tags.get(3, [0])[0], "")
        fid = tags.get(6)
        if not fid or len(fid) < 2:
            continue
        href = resolve(fid[0], fid[1])
        points.append((tags.get(4, [0])[0], NavPoint(label.strip() or "Untitled", href),
                       tags.get(21, [-1])[0], number))
    return _nest(points)



def _nest(points: list[tuple[int, NavPoint, int, int]]) -> list[NavPoint]:
    """Contents entries into a tree, by each entry's parent entry number."""
    by_number = {number: point for _, point, _, number in points}
    roots: list[NavPoint] = []
    for depth, point, parent, number in points:
        if depth > 0 and parent in by_number and parent != number:
            by_number[parent].children.append(point)
        else:
            roots.append(point)
    return roots


# ------------------------------------------------------------------ MOBI 6


_FILEPOS = re.compile(rb"""filepos\s*=\s*['"]?0*(\d+)['"]?""", re.I)
_RECINDEX = re.compile(rb"""recindex\s*=\s*['"]?0*(\d+)['"]?""", re.I)
_PAGEBREAK = re.compile(rb"<mbp:pagebreak\s*/?>", re.I)


def _mobi6_book(db: PalmDB, h: Header) -> Book:
    raw = read_text(db, h, 0)
    resources = Resources(db, h.first_resource, len(db))

    # Anchors where links point, placed before any tag they would fall inside
    targets = sorted({int(m.group(1)) for m in _FILEPOS.finditer(raw) if int(m.group(1)) < len(raw)})
    nav_entries, nav_strings = ([], {})
    if h.ncx_index != NO_LINK:
        try:
            nav_entries, nav_strings = read_index(db, h.ncx_index)
        except KindleError:
            nav_entries = []
    for _, tags in nav_entries:
        if 1 in tags and tags[1][0] < len(raw):
            targets.append(tags[1][0])
    targets = sorted(set(targets))
    pieces, last = [], 0
    for pos in targets:
        at = pos
        lt, gt = raw.rfind(b"<", 0, pos), raw.rfind(b">", 0, pos)
        if lt > gt:
            at = lt
        pieces.append(raw[last:at])
        pieces.append(b'<a id="filepos%d"></a>' % pos)
        last = at
    pieces.append(raw[last:])
    marked = b"".join(pieces)

    # One file per page break at the top of the body
    body_start = re.search(rb"<body[^>]*>", marked, re.I)
    body_end = marked.rfind(b"</body>")
    inner = marked[body_start.end() if body_start else 0:body_end if body_end != -1 else len(marked)]
    chunks = [c for c in _PAGEBREAK.split(inner) if c.strip()]
    if not chunks:
        chunks = [inner]
    names = [f"part{n:04d}.xhtml" for n in range(len(chunks))]
    where: dict[int, str] = {}
    for n, chunk in enumerate(chunks):
        for m in re.finditer(rb'<a id="filepos(\d+)"></a>', chunk):
            where[int(m.group(1))] = names[n]

    used: dict[int, Resource] = {}

    def link(m: re.Match) -> bytes:
        pos = int(m.group(1))
        name = where.get(pos)
        if name is None:
            near = [p for p in where if p <= pos]
            name = where[max(near)] if near else names[0]
        return f'href="{name}#filepos{pos}"'.encode()

    def image(m: re.Match) -> bytes:
        n = int(m.group(1))
        res = resources.get(n)
        if not res:
            return b""
        used[n] = res
        return f'src="{res.name}"'.encode()

    chapters = []
    for n, chunk in enumerate(chunks):
        chunk = _FILEPOS.sub(link, chunk)
        chunk = _RECINDEX.sub(image, chunk)
        doc = b"<html><head><title></title></head><body>" + chunk + b"</body></html>"
        root = lxml_html.document_fromstring(doc.decode(h.encoding, "replace"))
        chapters.append(Chapter(names[n], _as_xhtml(root)))

    nav: list[tuple[int, NavPoint, int, int]] = []
    for number, (_, tags) in enumerate(nav_entries):
        if 1 not in tags:
            continue
        pos = tags[1][0]
        name = where.get(pos) or names[0]
        label = nav_strings.get(tags.get(3, [0])[0], "").strip() or "Untitled"
        nav.append((tags.get(4, [0])[0], NavPoint(label, f"{name}#filepos{pos}"), tags.get(21, [-1])[0], number))
    cover = None
    if 201 in h.exth:
        res = resources.get(struct.unpack(">L", h.exth[201][0][:4])[0] + 1)
        if res:
            used[-1] = res
            cover = res.name
    return _finish(h, chapters, {}, list(used.values()), _nest(nav), cover)


# ------------------------------------------------------------------ shared


def _finish(h: Header, chapters, styles, resources, nav, cover) -> Book:
    title = (_exth_text(h, 503) or [h.title])[0] or "Untitled"
    authors = _exth_text(h, 100)
    language = (_exth_text(h, 524) or ["en"])[0]
    isbn = _exth_text(h, 104)
    asin = _exth_text(h, 113)
    digest = hashlib.sha1("\n".join([title, *authors]).encode("utf-8")).hexdigest()[:16]
    identifier = (isbn or asin or [f"kindle-{digest}"])[0]
    seen, unique = set(), []
    for r in resources:
        if r.name not in seen:
            seen.add(r.name)
            unique.append(r)
    for ch in chapters:
        t = ch.root.find(f".//{{{XHTML_NS}}}title")
        if t is not None and not (t.text or "").strip():
            t.text = title
    if not nav:
        nav = [NavPoint(_first_heading(ch) or f"Part {i + 1}", ch.name) for i, ch in enumerate(chapters)]
    return Book(
        title=title,
        authors=authors,
        language=language,
        identifier=identifier,
        chapters=chapters,
        styles=styles,
        resources=unique,
        nav=nav,
        cover=cover,
        description=(_exth_text(h, 103) or [""])[0],
        publisher=(_exth_text(h, 101) or [""])[0],
    )


def _first_heading(ch: Chapter) -> str:
    for tag in ("h1", "h2", "h3"):
        el = ch.root.find(f".//{{{XHTML_NS}}}{tag}")
        if el is not None:
            text = " ".join("".join(el.itertext()).split())
            if text:
                return text[:120]
    return ""


def read_book(data: bytes) -> Book:
    """Parse an AZW3 or MOBI file. Raises DrmProtected or KindleError."""
    db = PalmDB(data)
    h = _read_header(db.record(0))
    if h.encryption:
        raise DrmProtected("this book is protected with DRM")
    boundary = h.exth.get(121)
    if not h.kf8 and boundary:
        k = struct.unpack(">L", boundary[0][:4])[0]
        if k != NO_LINK and 0 < k < len(db):
            kf8 = _read_header(db.record(k))
            if kf8.encryption:
                raise DrmProtected("this book is protected with DRM")
            if kf8.kf8:
                # The KF8 half shares the first half's EXTH metadata when its own lacks it
                for key, value in h.exth.items():
                    kf8.exth.setdefault(key, value)
                return _kf8_book(db, kf8, k, first_resource=h.first_resource)
    if h.kf8:
        return _kf8_book(db, h, 0)
    return _mobi6_book(db, h)


# ------------------------------------------------------------------ EPUB


_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _zip_add(z: zipfile.ZipFile, name: str, data: bytes, compress: bool = True):
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    z.writestr(info, data)


def _xhtml_bytes(root: etree._Element) -> bytes:
    return b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n' + etree.tostring(
        root, encoding="utf-8", method="xml")


def _nav_doc(book: Book) -> bytes:
    def items(points: list[NavPoint], depth=0) -> str:
        if not points or depth > 12:
            return ""
        out = ["<ol>"]
        for p in points:
            out.append(f'<li><a href="{html.escape(p.href, quote=True)}">{html.escape(p.label)}</a>')
            out.append(items(p.children, depth + 1))
            out.append("</li>")
        out.append("</ol>")
        return "".join(out)

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}"><head><title>{html.escape(book.title)}</title></head>'
        f'<body><nav epub:type="toc" id="toc"><h1>Contents</h1>{items(book.nav)}</nav></body></html>'
    ).encode("utf-8")


def _ncx_doc(book: Book) -> bytes:
    counter = [0]

    def points(ps: list[NavPoint], depth=0) -> str:
        if depth > 12:
            return ""
        out = []
        for p in ps:
            counter[0] += 1
            n = counter[0]
            out.append(
                f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{html.escape(p.label)}</text></navLabel>'
                f'<content src="{html.escape(p.href, quote=True)}"/>{points(p.children, depth + 1)}</navPoint>')
        return "".join(out)

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head>'
        f'<meta name="dtb:uid" content="{html.escape(book.identifier)}"/></head>'
        f'<docTitle><text>{html.escape(book.title)}</text></docTitle><navMap>{points(book.nav)}</navMap></ncx>'
    ).encode("utf-8")


def write_epub(book: Book) -> bytes:
    """The book as an EPUB 3 file, byte for byte the same for the same book."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        _zip_add(z, "mimetype", b"application/epub+zip", compress=False)
        _zip_add(z, "META-INF/container.xml", (
            '<?xml version="1.0"?>\n<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>').encode())
        manifest, spine = [], []
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        manifest.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        for i, ch in enumerate(book.chapters):
            _zip_add(z, f"OEBPS/{ch.name}", _xhtml_bytes(ch.root))
            props = ' properties="svg"' if ch.root.find(f".//{{http://www.w3.org/2000/svg}}svg") is not None else ""
            manifest.append(f'<item id="c{i}" href="{ch.name}" media-type="application/xhtml+xml"{props}/>')
            spine.append(f'<itemref idref="c{i}"/>')
        for i, (name, css) in enumerate(sorted(book.styles.items())):
            _zip_add(z, f"OEBPS/{name}", css.encode("utf-8"))
            media = "image/svg+xml" if name.endswith(".svg") else "text/css"
            manifest.append(f'<item id="s{i}" href="{name}" media-type="{media}"/>')
        for i, r in enumerate(sorted(book.resources, key=lambda r: r.name)):
            _zip_add(z, f"OEBPS/{r.name}", r.data, compress=not r.media_type.startswith("image/jpeg"))
            props = ' properties="cover-image"' if r.name == book.cover else ""
            manifest.append(f'<item id="r{i}" href="{r.name}" media-type="{r.media_type}"{props}/>')
        _zip_add(z, "OEBPS/nav.xhtml", _nav_doc(book))
        _zip_add(z, "OEBPS/toc.ncx", _ncx_doc(book))
        meta = [
            f'<dc:identifier id="uid">{html.escape(book.identifier)}</dc:identifier>',
            f"<dc:title>{html.escape(book.title)}</dc:title>",
            f"<dc:language>{html.escape(book.language)}</dc:language>",
            '<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>',
        ]
        meta += [f"<dc:creator>{html.escape(a)}</dc:creator>" for a in book.authors]
        if book.publisher:
            meta.append(f"<dc:publisher>{html.escape(book.publisher)}</dc:publisher>")
        if book.description:
            meta.append(f"<dc:description>{html.escape(book.description)}</dc:description>")
        if book.cover:
            cover_id = next(f"r{i}" for i, r in enumerate(sorted(book.resources, key=lambda r: r.name))
                            if r.name == book.cover)
            meta.append(f'<meta name="cover" content="{cover_id}"/>')
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
            f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{"".join(meta)}</metadata>'
            f'<manifest>{"".join(manifest)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>'
        )
        _zip_add(z, "OEBPS/content.opf", opf.encode("utf-8"))
    return buf.getvalue()



def convert_to_epub(data: bytes) -> bytes:
    """AZW3 or MOBI bytes to EPUB bytes. Raises DrmProtected or KindleError."""
    try:
        return write_epub(read_book(data))
    except (DrmProtected, KindleError):
        raise
    except (struct.error, IndexError, ValueError, UnicodeError, etree.LxmlError) as e:
        raise KindleError(f"could not read this Kindle book: {e}") from e
