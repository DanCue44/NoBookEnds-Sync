"""Build tests/fixtures/alice.epub, a tiny EPUB 3 with public-domain text from
Lewis Carroll's "Alice's Adventures in Wonderland" (1865).

It deliberately exercises the awkward parts of CFI mapping: a nav document
that must be skipped, spine order that differs from manifest order, a content
file in a subfolder with a space in its name, inline markup, a comment that
splits a text node, a script element, entities and a non-BMP character.
"""

import zipfile
from pathlib import Path

OUT = Path(__file__).with_name("alice.epub")

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">nobookends-sync-fixture-alice</dc:identifier>
    <dc:title>Alice's Adventures in Wonderland (excerpt)</dc:title>
    <dc:creator>Lewis Carroll</dc:creator>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2026-10-13T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="ch2" href="text/chapter%202.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="nav" linear="no"/>
    <itemref idref="title"/>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol>
<li><a href="chapter1.xhtml">Down the Rabbit-Hole</a></li>
<li><a href="text/chapter%202.xhtml">The Pool of Tears</a></li>
</ol></nav></body></html>
"""

TITLE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Title</title></head>
<body>
<h1>Alice&#8217;s Adventures in Wonderland</h1>
<p>by Lewis Carroll</p>
</body></html>
"""

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter I</title><style>p { margin: 0 }</style></head>
<body id="ch1body">
<h2 id="c1">CHAPTER I.<br/>Down the Rabbit-Hole</h2>
<p id="p1">Alice was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do: once or twice she had peeped into the book her sister was reading, but it had no pictures or conversations in it, &#8220;and what is the use of a book,&#8221; thought Alice &#8220;without pictures or conversations?&#8221;</p>
<p>So she was considering in her own mind (as well as she could, for the hot day made her feel very sleepy and stupid), whether the pleasure of making a daisy-chain would be worth the trouble of getting up and picking the daisies, when suddenly a <em>White Rabbit</em> with pink eyes ran close by her.</p>
<script type="text/javascript">var ignored = "Text inside a script is never aligned.";</script>
<p>There was nothing so <i>very</i> remarkable in that; nor did Alice think it so <i>very</i> much out of the way to hear the Rabbit say to itself, &#8220;Oh dear! Oh dear! I shall be late!&#8221; (when she thought it over afterwards, it occurred to her that she ought to have wondered at this, but at the time it all seemed quite natural); but when the Rabbit actually <em>took a watch out of its waistcoat-pocket</em>, and looked at it, and then hurried on, Alice started to her feet.</p>
<p>In another moment down went Alice after it, never once <!-- a comment splits this text node -->considering how in the world she was to get out again.</p>
<p>The rabbit-hole went straight on like a tunnel for some way, and then dipped suddenly down, so suddenly that Alice had not a moment to think about stopping herself before she found herself falling down a very deep well. \U0001F407 Mr. Rabbit was nowhere to be seen.</p>
</body></html>
"""

CH2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter II</title></head>
<body>
<section>
<h2>CHAPTER II.<br/>The Pool of Tears</h2>
<div class="text">
<p>&#8220;Curiouser and curiouser!&#8221; cried Alice (she was so much surprised, that for the moment she quite forgot how to speak good English); &#8220;now I&#8217;m opening out like the largest telescope that ever was! Good-bye, feet!&#8221;</p>
<p>For when she looked down at her feet, they seemed to be almost out of sight, they were getting so far off. &#8220;Oh, my poor little feet, I wonder who will put on your shoes and stockings for you now, dears?&#8221;</p>
</div>
</section>
</body></html>
"""


def build(out: Path = OUT) -> Path:
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", OPF, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", NAV, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/title.xhtml", TITLE, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/chapter1.xhtml", CH1, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/text/chapter 2.xhtml", CH2, compress_type=zipfile.ZIP_DEFLATED)
    return out


if __name__ == "__main__":
    print(build())
