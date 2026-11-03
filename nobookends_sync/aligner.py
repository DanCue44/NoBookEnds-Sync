"""Align transcript words (with timestamps) to book words.

Strategy (robust to ASR errors, narrator intros/outros, credits, chapter titles
read aloud, skipped front matter and abridgement):

1. Anchors. Find word n-grams that occur exactly once in the transcript region
   and exactly once in the book region. Keep the longest chain of them that is
   increasing in both sequences (LIS). Because the chain must be monotonic,
   scattered coincidental matches lose against the long run of true matches,
   and whole skipped book sections (abridgement) simply become jumps.
2. Recurse. Between consecutive anchors, look again for anchors that are
   unique *within that smaller region*, with shorter n-grams, until regions
   are small.
3. Dynamic programming. Small regions are aligned with an affine-gap
   (Gotoh) global alignment that scores exact and fuzzy word matches. Gaps in
   the book are cheap to extend (abridgement), gaps in the transcript cost
   more (ASR insertions are usually short). The open ends before the first and
   after the last anchor use free end gaps.
4. Cleanup. Tiny islands of matches far from any other match are dropped.

Book words then take the timing of their matched transcript words; short runs
of unmatched book words between matches are interpolated by character length.
A sentence becomes a segment when enough of its words matched. Everything else
is a gap.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

from .epub import Book, sentence_text
from .textnorm import tokenize

EXACT, FUZZY = 2, 1

DP_MAX_CELLS = 400_000
MAX_INTERP_TOKENS = 12
SENTENCE_MIN_RATIO = 0.4
SECONDS_PER_CHAR = 0.065
WORD_MAX_BASE_SEC = 0.2
WORD_MAX_SEC_PER_CHAR = 0.07


@dataclass
class AsrToken:
    norm: str
    t0: float
    t1: float


def asr_tokens_from_words(words) -> list[AsrToken]:
    """words: iterable of (text, t0, t1). A whisper word may hold several
    tokens ("well-known", "1990s"); split its time span by character share."""
    out: list[AsrToken] = []
    for text, t0, t1 in words:
        toks = tokenize(text)
        if not toks:
            continue
        # Whisper often stretches the first word after a pause back over the
        # silence ("He" lasting a full second). Cap a word's duration by its
        # length so the start lands near the actual onset.
        cap = WORD_MAX_BASE_SEC + WORD_MAX_SEC_PER_CHAR * sum(len(n) for n, _, _ in toks)
        if t1 - t0 > cap:
            t0 = t1 - cap
        total = sum(len(n) for n, _, _ in toks) or 1
        cur = t0
        for n, _, _ in toks:
            dur = (t1 - t0) * len(n) / total
            out.append(AsrToken(n, cur, cur + dur))
            cur += dur
    return out


# ---------------------------------------------------------------- anchors


def _unique_ngrams(seq, lo, hi, n):
    seen: dict[tuple, int] = {}
    dup = set()
    for k in range(lo, hi - n + 1):
        g = tuple(seq[k:k + n])
        if g in seen:
            dup.add(g)
        else:
            seen[g] = k
    for g in dup:
        del seen[g]
    return seen


_STOP = {
    "the", "a", "an", "and", "of", "to", "in", "he", "she", "it", "was", "that",
    "i", "you", "his", "her", "for", "on", "with", "as", "at", "is", "had", "be",
    "they", "but", "said", "not", "this", "by", "we", "from", "or", "have",
}


def _lis(pairs):
    """Longest chain strictly increasing in both coordinates. pairs sorted by i."""
    tails: list[int] = []
    tails_idx: list[int] = []
    prev = [-1] * len(pairs)
    for k, (_, j) in enumerate(pairs):
        pos = bisect.bisect_left(tails, j)
        if pos == len(tails):
            tails.append(j)
            tails_idx.append(k)
        else:
            tails[pos] = j
            tails_idx[pos] = k
        prev[k] = tails_idx[pos - 1] if pos > 0 else -1
    out = []
    k = tails_idx[-1] if tails_idx else -1
    while k >= 0:
        out.append(pairs[k])
        k = prev[k]
    out.reverse()
    return out


def find_anchors(asr, book, a0, a1, b0, b1, n):
    """Exact-match anchor pairs (i, j) in the region, monotonic."""
    if a1 - a0 < n or b1 - b0 < n:
        return []
    ug_a = _unique_ngrams(asr, a0, a1, n)
    ug_b = _unique_ngrams(book, b0, b1, n)
    cands = []
    for g, i in ug_a.items():
        j = ug_b.get(g)
        if j is None:
            continue
        if n <= 3 and all(w in _STOP for w in g):
            continue
        cands.append((i, j))
    cands.sort()
    chain = _lis(cands)
    pairs = []
    last_i = last_j = -1
    for i, j in chain:
        # extend the n-gram backwards and forwards while words keep matching
        s = 0
        while i - s - 1 > max(last_i, a0 - 1) and j - s - 1 > max(last_j, b0 - 1) and asr[i - s - 1] == book[j - s - 1]:
            s += 1
        for k in range(-s, n):
            if i + k > last_i and j + k > last_j:
                pairs.append((i + k, j + k))
                last_i, last_j = i + k, j + k
        e = n
        while i + e < a1 and j + e < b1 and asr[i + e] == book[j + e]:
            pairs.append((i + e, j + e))
            last_i, last_j = i + e, j + e
            e += 1
    return pairs


# ---------------------------------------------------------------- DP


def _sim(a: str, b: str) -> int:
    if a == b:
        return EXACT
    if len(a) >= 3 and len(b) >= 3 and a[0] == b[0] and Levenshtein.normalized_similarity(a, b) >= 0.75:
        return FUZZY
    return 0


S_EXACT, S_FUZZY, S_MISMATCH = 2.0, 1.0, -1.0
GAP_OPEN = -3.0
GAP_EXT_BOOK = -0.05   # skipping book words (abridgement, front matter)
GAP_EXT_ASR = -0.5     # skipping transcript words (intros, ASR insertions)
NEG = -1e18


def dp_align(asr, book, a0, a1, b0, b1, free_start=False, free_end=False):
    """Gotoh global alignment of asr[a0:a1] vs book[b0:b1]. Returns matched
    (i, j, kind) for exact/fuzzy aligned pairs only."""
    n, m = a1 - a0, b1 - b0
    if n == 0 or m == 0:
        return []
    A = asr[a0:a1]
    B = book[b0:b1]
    # M: ends with a pair; X: ends with gap in book side (asr word skipped);
    # Y: ends with gap in asr side (book word skipped).
    prevM = [NEG] * (m + 1)
    prevX = [NEG] * (m + 1)
    prevY = [NEG] * (m + 1)
    prevM[0] = 0.0
    for j in range(1, m + 1):
        prevY[j] = 0.0 if free_start else GAP_OPEN + GAP_EXT_BOOK * j
    lastcol = [(prevM[m], prevX[m], prevY[m])]  # per row values at column m
    tb = []  # per row: bytes of back pointers for M, X, Y
    tb.append((bytes(m + 1), bytes(m + 1), bytes(m + 1)))
    for i in range(1, n + 1):
        curM = [NEG] * (m + 1)
        curX = [NEG] * (m + 1)
        curY = [NEG] * (m + 1)
        bM = bytearray(m + 1)
        bX = bytearray(m + 1)
        bY = bytearray(m + 1)
        curX[0] = 0.0 if free_start else GAP_OPEN + GAP_EXT_ASR * i
        bX[0] = 1
        ai = A[i - 1]
        for j in range(1, m + 1):
            # M
            s = _sim(ai, B[j - 1])
            sc = S_EXACT if s == EXACT else (S_FUZZY if s == FUZZY else S_MISMATCH)
            pm, px, py = prevM[j - 1], prevX[j - 1], prevY[j - 1]
            if pm >= px and pm >= py:
                curM[j] = pm + sc
                bM[j] = 0
            elif px >= py:
                curM[j] = px + sc
                bM[j] = 1
            else:
                curM[j] = py + sc
                bM[j] = 2
            # X: consume asr word, gap in book (vertical move)
            o = prevM[j] + GAP_OPEN + GAP_EXT_ASR
            e = prevX[j] + GAP_EXT_ASR
            y = prevY[j] + GAP_OPEN + GAP_EXT_ASR
            if o >= e and o >= y:
                curX[j] = o
                bX[j] = 0
            elif e >= y:
                curX[j] = e
                bX[j] = 1
            else:
                curX[j] = y
                bX[j] = 2
            # Y: consume book word, gap in asr (horizontal move)
            o = curM[j - 1] + GAP_OPEN + GAP_EXT_BOOK
            e = curY[j - 1] + GAP_EXT_BOOK
            x = curX[j - 1] + GAP_OPEN + GAP_EXT_BOOK
            if o >= e and o >= x:
                curY[j] = o
                bY[j] = 0
            elif e >= x:
                curY[j] = e
                bY[j] = 2
            else:
                curY[j] = x
                bY[j] = 1
        tb.append((bytes(bM), bytes(bX), bytes(bY)))
        prevM, prevX, prevY = curM, curX, curY
        lastcol.append((curM[m], curX[m], curY[m]))

    # choose end cell
    if free_end:
        best, bi, bj, state = NEG, n, m, 0
        # last row: trailing book words free; last column: trailing asr free
        for j in range(m + 1):
            for st, arr in ((0, prevM), (1, prevX), (2, prevY)):
                if arr[j] > best:
                    best, bi, bj, state = arr[j], n, j, st
        for i in range(n + 1):
            for st in range(3):
                if lastcol[i][st] > best:
                    best, bi, bj, state = lastcol[i][st], i, m, st
    else:
        bi, bj = n, m
        vals = (prevM[m], prevX[m], prevY[m])
        state = max(range(3), key=lambda k: vals[k])

    out = []
    i, j = bi, bj
    while i > 0 and j > 0:
        bM, bX, bY = tb[i]
        if state == 0:
            s = _sim(A[i - 1], B[j - 1])
            if s:
                out.append((a0 + i - 1, b0 + j - 1, s))
            state = bM[j]
            i -= 1
            j -= 1
        elif state == 1:
            state = bX[j]
            i -= 1
        else:
            state = bY[j]
            j -= 1
    out.reverse()
    return out


# ---------------------------------------------------------------- driver


def align_sequences(asr: list[str], book: list[str], dp_max_cells: int = DP_MAX_CELLS):
    """Return monotonic (i, j, kind) matches between asr and book tokens."""
    result: list[tuple[int, int, int]] = []

    def solve(a0, a1, b0, b1, ngram, free_start, free_end, depth):
        na, nb = a1 - a0, b1 - b0
        if na <= 0 or nb <= 0:
            return
        if na * nb <= dp_max_cells:
            result.extend(dp_align(asr, book, a0, a1, b0, b1, free_start, free_end))
            return
        anchors = []
        for n in (ngram, 4, 3) if ngram > 4 else (ngram, 3):
            if n < 3:
                break
            anchors = find_anchors(asr, book, a0, a1, b0, b1, n)
            if anchors:
                break
        if not anchors:
            # Nothing reliable in a large region (e.g. a long intro with no
            # book text). Try the open-ended DP on a trimmed window, else gap.
            if free_start and na * min(nb, 3 * na + 50) <= dp_max_cells:
                lo = max(b0, b1 - (3 * na + 50))
                result.extend(dp_align(asr, book, a0, a1, lo, b1, True, False))
            elif free_end and na * min(nb, 3 * na + 50) <= dp_max_cells:
                hi = min(b1, b0 + 3 * na + 50)
                result.extend(dp_align(asr, book, a0, a1, b0, hi, False, True))
            return
        # head region (before first anchor)
        fi, fj = anchors[0]
        if free_start:
            # only look at a window of the book before the first anchor
            span = fi - a0
            lo = max(b0, fj - (3 * span + 50))
            solve(a0, fi, lo, fj, 4, True, False, depth + 1)
        else:
            solve(a0, fi, b0, fj, 4, False, False, depth + 1)
        for (pi, pj), (ni, nj) in zip(anchors, anchors[1:]):
            result.append((pi, pj, EXACT))
            if ni - pi > 1 and nj - pj > 1:
                solve(pi + 1, ni, pj + 1, nj, 4, False, False, depth + 1)
        li, lj = anchors[-1]
        result.append((li, lj, EXACT))
        if free_end:
            span = a1 - li - 1
            hi = min(b1, lj + 1 + 3 * span + 50)
            solve(li + 1, a1, lj + 1, hi, 4, False, True, depth + 1)
        else:
            solve(li + 1, a1, lj + 1, b1, 4, False, False, depth + 1)

    solve(0, len(asr), 0, len(book), 6, True, True, 0)
    result.sort()
    # enforce strict monotonicity
    clean = []
    li = lj = -1
    for i, j, k in result:
        if i > li and j > lj:
            clean.append((i, j, k))
            li, lj = i, j
    return _drop_islands(clean)


def _drop_islands(pairs, min_run=3, far=25):
    """Remove short runs of matches that are far from any other match."""
    if not pairs:
        return pairs
    runs = []
    start = 0
    for k in range(1, len(pairs) + 1):
        if k == len(pairs) or pairs[k][0] - pairs[k - 1][0] > 3 or pairs[k][1] - pairs[k - 1][1] > 3:
            runs.append((start, k))
            start = k
    keep = []
    for r, (s, e) in enumerate(runs):
        if e - s >= min_run:
            keep.extend(pairs[s:e])
            continue
        near = False
        if r > 0:
            ps, pe = runs[r - 1]
            near |= pairs[s][1] - pairs[pe - 1][1] <= far and pairs[s][0] - pairs[pe - 1][0] <= far and pe - ps >= min_run
        if r + 1 < len(runs):
            ns, ne = runs[r + 1]
            near |= pairs[ns][1] - pairs[e - 1][1] <= far and pairs[ns][0] - pairs[e - 1][0] <= far and ne - ns >= min_run
        if near:
            keep.extend(pairs[s:e])
    return keep


def count_anchor_hits(asr: list[str], book: list[str], n: int = 5) -> int:
    """Cheap check that the narration belongs to this book."""
    return len(_lis(sorted(
        (i, j) for g, i in _unique_ngrams(asr, 0, len(asr), n).items()
        if (j := _BookIndexCache.get(book, n).get(g)) is not None
    )))


class _BookIndexCache:
    _key = None
    _val = None

    @classmethod
    def get(cls, book, n):
        key = (id(book), len(book), n)
        if cls._key != key:
            cls._val = _unique_ngrams(book, 0, len(book), n)
            cls._key = key
        return cls._val


# ---------------------------------------------------------------- segments


def build_segments(book: Book, asr: list[AsrToken], pairs):
    """Turn matches into contract segments and a quality report."""
    nb = len(book.tokens)
    t0 = [None] * nb
    t1 = [None] * nb
    matched = bytearray(nb)
    for i, j, kind in pairs:
        t0[j] = asr[i].t0
        t1[j] = asr[i].t1
        matched[j] = kind

    # interpolate short runs of unmatched book tokens between matches
    mj = [j for j in range(nb) if matched[j]]
    for ja, jb in zip(mj, mj[1:]):
        gap = jb - ja - 1
        if gap == 0 or gap > MAX_INTERP_TOKENS:
            continue
        ta, tb = t1[ja], t0[jb]
        if tb < ta or tb - ta > 1.5 * gap + 2.0:
            continue
        lens = [book.tok_end[j] - book.tok_start[j] for j in range(ja + 1, jb)]
        total = sum(lens) or 1
        cur = ta
        for j, ln in zip(range(ja + 1, jb), lens):
            d = (tb - ta) * ln / total
            t0[j], t1[j] = cur, cur + d
            cur += d

    segments = []
    last_t1 = 0.0
    covered_tokens = 0
    for si, s in enumerate(book.sentences):
        idx = range(s.tok_first, s.tok_last)
        n = len(idx)
        nm = sum(1 for j in idx if matched[j])
        if nm == 0:
            continue
        all_timed = all(t0[j] is not None for j in idx)
        if nm / n < SENTENCE_MIN_RATIO and not all_timed:
            continue
        # extrapolate untimed edge words from the nearest timed word
        times = [(t0[j], t1[j]) for j in idx]
        first = next(k for k, v in enumerate(times) if v[0] is not None)
        for k in range(first - 1, -1, -1):
            j = s.tok_first + k
            d = (book.tok_end[j] - book.tok_start[j]) * SECONDS_PER_CHAR
            end = times[k + 1][0]
            times[k] = (max(end - d, last_t1), end)
        for k in range(1, n):
            if times[k][0] is None:
                j = s.tok_first + k
                d = (book.tok_end[j] - book.tok_start[j]) * SECONDS_PER_CHAR
                st = times[k - 1][1]
                times[k] = (st, st + d)
        # monotonic clean up
        fixed = []
        prev = last_t1
        for a, b in times:
            a = max(a, prev)
            b = max(b, a)
            fixed.append((a, b))
            prev = a
        doc = book.docs[s.doc]
        text = sentence_text(doc, s)
        base = doc.utf16(s.start)
        words = []
        for k, j in enumerate(idx):
            words.append({
                "t0": round(fixed[k][0], 3),
                "t1": round(fixed[k][1], 3),
                "start": doc.utf16(book.tok_start[j]) - base,
                "end": doc.utf16(book.tok_end[j]) - base,
            })
        seg_t0, seg_t1 = fixed[0][0], max(b for _, b in fixed)
        segments.append({
            "id": si,
            "t0": round(seg_t0, 3),
            "t1": round(seg_t1, 3),
            "href": doc.href,
            "cfiStart": doc.cfi(s.start),
            "cfiEnd": doc.cfi(s.end, end=True),
            "text": text,
            "textStart": base,
            "textEnd": doc.utf16(s.end),
            "words": words,
        })
        last_t1 = seg_t1
        covered_tokens += n

    n_exact = sum(1 for _, _, k in pairs if k == EXACT)
    speech = sum(max(0.0, a.t1 - a.t0) for a in asr) or 1.0
    in_seg = 0.0
    if segments:
        starts = [sg["t0"] for sg in segments]
        for a in asr:
            k = bisect.bisect_right(starts, a.t0) - 1
            if k >= 0 and a.t0 <= segments[k]["t1"] + 0.05:
                in_seg += max(0.0, a.t1 - a.t0)
    quality = {
        "score": round(len(pairs) / max(1, len(asr)), 4),
        "asrTokens": len(asr),
        "matchedTokens": len(pairs),
        "exactTokens": n_exact,
        "bookTokens": nb,
        "bookTokensCovered": covered_tokens,
        "bookCoverage": round(covered_tokens / max(1, nb), 4),
        "speechCoverage": round(min(1.0, in_seg / speech), 4),
        "segments": len(segments),
        "sentences": len(book.sentences),
    }
    return segments, quality


def align_book(book: Book, asr: list[AsrToken]):
    pairs = align_sequences([a.norm for a in asr], book.tokens)
    return build_segments(book, asr, pairs)

