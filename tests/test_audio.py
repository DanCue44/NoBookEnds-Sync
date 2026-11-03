"""Chunked decoding: skipping to a checkpoint yields exactly the same samples."""

import numpy as np

from nobookends_sync.audio import SR, iter_chunks


def _wav(path, seconds=7.0):
    import av

    rate = 22050
    t = np.arange(int(seconds * rate)) / rate
    pcm = (np.sin(2 * np.pi * 220 * t) * (0.2 + 0.6 * t / seconds) * 32767).astype(np.int16)
    with av.open(str(path), "w") as out:
        st = out.add_stream("pcm_s16le", rate=rate, layout="mono")
        for i in range(0, pcm.size, 1024):
            fr = av.AudioFrame.from_ndarray(pcm[None, i:i + 1024], format="s16", layout="mono")
            fr.sample_rate = rate
            for pkt in st.encode(fr):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)


def test_start_sec_skips_exactly(tmp_path):
    p = tmp_path / "a.wav"
    _wav(p)
    whole = np.concatenate([c for _, c in iter_chunks(str(p), chunk_sec=100)])
    chunks = list(iter_chunks(str(p), chunk_sec=2.0, overlap_sec=0.5, start_sec=2.25))
    assert chunks[0][0] == 2.25
    assert [round(s, 2) for s, _ in chunks] == [2.25, 3.75, 5.25]
    for start, data in chunks:
        i = round(start * SR)
        assert np.array_equal(data, whole[i:i + data.size])
    assert chunks[-1][0] + chunks[-1][1].size / SR == whole.size / SR
    assert list(iter_chunks(str(p), start_sec=100.0)) == []
