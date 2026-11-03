"""Tokenization and normalization shared by the book text and the transcript.

Both sides must be tokenized identically so that equal words compare equal.
A token is a run of letters/digits, optionally joined by apostrophes
("don't", "Jobs's"). Hyphens, dashes and other punctuation split tokens.
Normalization lowercases, strips accents and drops apostrophes.
"""

from __future__ import annotations

import re
import unicodedata

_APOS = "'’ʼ‘"
TOKEN_RE = re.compile(r"[^\W_]+(?:[" + _APOS + r"][^\W_]+)*")
_APOS_RE = re.compile("[" + _APOS + "]")

# A few spoken forms that ASR and print disagree on often enough to matter.
_EQUIV = {
    "ok": "okay",
    "mr": "mister",
    "mrs": "missus",
    "dr": "doctor",
    "st": "saint",
    "vs": "versus",
    "percent": "percent",
}


def normalize(word: str) -> str:
    w = unicodedata.normalize("NFKD", word)
    w = "".join(c for c in w if not unicodedata.combining(c))
    w = _APOS_RE.sub("", w).lower()
    return _EQUIV.get(w, w)


def tokenize(text: str) -> list[tuple[str, int, int]]:
    """Return (normalized, start, end) for each token, offsets in code points."""
    out = []
    for m in TOKEN_RE.finditer(text):
        n = normalize(m.group(0))
        if n:
            out.append((n, m.start(), m.end()))
    return out


def utf16_len(s: str) -> int:
    """Length of s in UTF-16 code units (what JavaScript and the DOM count)."""
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)


class Utf16Index:
    """Maps code point offsets of a string to UTF-16 offsets."""

    def __init__(self, s: str):
        self._astral = [i for i, c in enumerate(s) if ord(c) > 0xFFFF]

    def __call__(self, cp: int) -> int:
        if not self._astral:
            return cp
        import bisect

        return cp + bisect.bisect_left(self._astral, cp)
