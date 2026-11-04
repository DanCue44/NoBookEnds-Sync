"""Build the Kindle fixtures in this folder from public-domain text.

rich.epub is written here (two chapters, a nested contents list, a footnote
link between files, a stylesheet and a small PNG). The AZW3 and MOBI copies are
made from it and from ../alice.epub with Calibre's ebook-convert, which is only
needed to rebuild them:

    python tests/fixtures/kindle/build_kindle_fixtures.py [path/to/ebook-convert]
"""

import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).parent
CONVERT = sys.argv[1] if len(sys.argv) > 1 else "ebook-convert"


def png(width=8, height=8) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\xec\x70\x3e" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


CH1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter One</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head><body>
<h1 id="one">Chapter One</h1>
<p>It is a truth universally acknowledged, that a single man in possession of a good fortune,
must be in want of a wife.<a href="ch2.xhtml#note1">1</a></p>
<p><img src="dot.png" alt="A small orange square"/></p>
<h2 id="one-a">A Smaller Part</h2>
<p>However little known the feelings or views of such a man may be on his first entering a
neighbourhood, this truth is so well fixed in the minds of the surrounding families.</p>
</body></html>
"""

CH2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter Two</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head><body>
<h1 id="two">Chapter Two</h1>
<p>Mr. Bennet was among the earliest of those who waited on Mr. Bingley.</p>
<p id="note1" class="note">1. From Jane Austen, Pride and Prejudice (1813).</p>
</body></html>
"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head>
<body><nav epub:type="toc"><ol>
<li><a href="ch1.xhtml#one">Chapter One</a><ol><li><a href="ch1.xhtml#one-a">A Smaller Part</a></li></ol></li>
<li><a href="ch2.xhtml#two">Chapter Two</a></li>
</ol></nav></body></html>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">nobookends-sync-fixture-rich</dc:identifier>
<dc:title>Pride and Prejudice (excerpt)</dc:title><dc:creator>Jane Austen</dc:creator>
<dc:language>en</dc:language><meta property="dcterms:modified">2026-11-03T00:00:00Z</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
<item id="dot" href="dot.png" media-type="image/png"/>
</manifest>
<spine><itemref idref="ch1"/><itemref idref="ch2"/></spine>
</package>
"""

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def build_rich():
    out = HERE / "rich.epub"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype", (1980, 1, 1, 0, 0, 0)), "application/epub+zip")
        for name, data in [("META-INF/container.xml", CONTAINER), ("OEBPS/content.opf", OPF),
                           ("OEBPS/nav.xhtml", NAV), ("OEBPS/ch1.xhtml", CH1), ("OEBPS/ch2.xhtml", CH2),
                           ("OEBPS/style.css", "p.note { font-size: 0.8em; }\n"), ("OEBPS/dot.png", png())]:
            z.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return out


def convert(src: Path, dest: str, *extra: str):
    subprocess.run([CONVERT, str(src), str(HERE / dest), *extra], check=True, capture_output=True)


if __name__ == "__main__":
    rich = build_rich()
    alice = HERE.parent / "alice.epub"
    convert(rich, "rich.azw3")
    convert(rich, "rich.mobi")
    convert(rich, "rich-both.mobi", "--mobi-file-type", "both")
    convert(alice, "alice.azw3")
    convert(alice, "alice.mobi", "--dont-compress")
    print("built", ", ".join(sorted(p.name for p in HERE.iterdir() if p.suffix in (".azw3", ".mobi", ".epub"))))
