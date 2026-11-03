import random

from nobookends_sync.aligner import (
    AsrToken,
    align_book,
    align_sequences,
    asr_tokens_from_words,
    count_anchor_hits,
    dp_align,
)
from nobookends_sync.epub import sentence_text


def _noisy_copy(book, truth_idx, rng, vocab, p_del=0.05, p_sub=0.05, p_ins=0.03):
    asr, gt = [], []
    for j in truth_idx:
        r = rng.random()
        if r < p_del:
            continue
        if r < p_del + p_sub:
            asr.append(rng.choice(vocab))
            gt.append(None)
            continue
        if r < p_del + p_sub + p_ins:
            asr.append(rng.choice(vocab))
            gt.append(None)
        asr.append(book[j])
        gt.append(j)
    return asr, gt


def test_synthetic_noise_intro_outro_and_abridgement():
    rng = random.Random(7)
    vocab = [f"w{i}" for i in range(4000)] + ["the", "and", "of", "a", "to"] * 400
    book = [rng.choice(vocab) for _ in range(30000)]
    # skipped front matter, an abridged middle section and an unread ending
    truth = list(range(300, 8000)) + list(range(12000, 26000))
    intro = [rng.choice(vocab) for _ in range(120)]
    outro = [rng.choice(vocab) for _ in range(120)]
    body, gt_body = _noisy_copy(book, truth, rng, vocab)
    asr = intro + body + outro
    gt = [None] * len(intro) + gt_body + [None] * len(outro)

    pairs = align_sequences(asr, book)
    correct = sum(1 for i, j, _ in pairs if gt[i] == j)
    possible = sum(1 for g in gt if g is not None)
    assert correct / len(pairs) > 0.99          # precision
    assert correct / possible > 0.97            # recall
    # nothing from the abridged gap or the unread tail gets matched
    assert not any(8000 + 20 <= j < 12000 - 20 for _, j, _ in pairs)
    assert not any(j >= 26000 + 20 for _, j, _ in pairs)
    # monotonic
    assert all(a[0] < b[0] and a[1] < b[1] for a, b in zip(pairs, pairs[1:]))


def test_dp_handles_fuzzy_words():
    book = "the quick brown fox jumped over the lazy dog".split()
    asr = "the quick browne fox jumps over a lazy dog".split()
    pairs = dp_align(asr, book, 0, len(asr), 0, len(book))
    got = {(i, j) for i, j, _ in pairs}
    assert (2, 2) in got  # browne ~ brown
    assert (0, 0) in got and (8, 8) in got
    assert all(i == j for i, j in got)


def test_asr_tokens_split_time_by_chars():
    toks = asr_tokens_from_words([("well-known", 1.0, 1.8), ("Jobs's", 1.8, 2.3), ("...", 2.3, 2.4)])
    assert [t.norm for t in toks] == ["well", "known", "jobss"]
    assert abs(toks[0].t1 - (1.0 + 0.8 * 4 / 9)) < 1e-9 and abs(toks[1].t0 - toks[0].t1) < 1e-9


def test_word_absorbing_a_pause_is_trimmed():
    toks = asr_tokens_from_words([("century.", 185.42, 185.82), ("He", 185.82, 186.76), ("had", 186.95, 187.09)])
    assert toks[0].t0 == 185.42                      # normal word untouched
    assert abs(toks[1].t0 - (186.76 - 0.34)) < 1e-9  # 0.2 + 2 * 0.07
    assert toks[1].t1 == 186.76


def _fake_narration(book, sentence_ids, rng, wps=2.8, noise=0.0, intro=()):
    """Simulated ASR over the given sentences; returns tokens and true word times."""
    t = 0.0
    out = []
    truth = {}
    for w in intro:
        out.append(AsrToken(w, t, t + 0.3))
        t += 0.35
    for si in sentence_ids:
        s = book.sentences[si]
        for j in range(s.tok_first, s.tok_last):
            d = 1.0 / wps
            truth[j] = (t, t + d * 0.9)
            if rng.random() < noise:
                out.append(AsrToken("zzz" + book.tokens[j], t, t + d * 0.9))
            else:
                out.append(AsrToken(book.tokens[j], t, t + d * 0.9))
            t += d
        t += 0.6  # pause between sentences
    return out, truth


def test_align_book_segments_and_gaps(book):
    rng = random.Random(1)
    n = len(book.sentences)
    skipped = {0, 1, 9}  # title page not read, one sentence abridged away
    read = [i for i in range(n) if i not in skipped]
    asr, truth = _fake_narration(book, read, rng, noise=0.08,
                                 intro="this is an audible production read by somebody".split())
    segments, quality = align_book(book, asr)
    asr_by_time = {round(a.t0, 6): a.norm for a in asr}
    ids = [s["id"] for s in segments]
    assert not (set(ids) & skipped)
    assert len(ids) >= len(read) - 1
    assert quality["score"] > 0.85
    for seg in segments:
        s = book.sentences[seg["id"]]
        j0 = s.tok_first
        first_word_heard = asr_by_time.get(round(truth[j0][0], 6)) == book.tokens[j0]
        assert abs(seg["t0"] - truth[j0][0]) < (0.01 if first_word_heard else 1.0)
        assert seg["t0"] <= seg["t1"]
        # word offsets point at the right words inside the segment text
        text = seg["text"]
        assert text == sentence_text(book.docs[s.doc], s)
        assert len(seg["words"]) == s.tok_last - s.tok_first
        for k, w in enumerate(seg["words"]):
            assert 0 <= w["start"] < w["end"] <= len(text.encode("utf-16-le")) // 2
    # ordered by time, non-overlapping starts
    assert all(a["t0"] <= b["t0"] for a, b in zip(segments, segments[1:]))


def test_count_anchor_hits_detects_wrong_book(book):
    rng = random.Random(3)
    asr, _ = _fake_narration(book, range(len(book.sentences)), rng)
    toks = [a.norm for a in asr]
    assert count_anchor_hits(toks, book.tokens) > 20
    other = [f"x{rng.randint(0, 500)}" for _ in range(len(toks))]
    assert count_anchor_hits(other, book.tokens) == 0
