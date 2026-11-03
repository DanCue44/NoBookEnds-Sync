"""faster-whisper wrapper producing word timestamps."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings

log = logging.getLogger("nbs.transcriber")


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


@dataclass
class Word:
    text: str
    t0: float
    t1: float
    p: float


class Transcriber:
    def __init__(self, settings: Settings):
        from faster_whisper import WhisperModel

        self.s = settings
        want = settings.whisper_device
        device = "cuda" if want == "cuda" or (want == "auto" and cuda_available()) else "cpu"
        compute = settings.whisper_compute_type if device == "cuda" else settings.whisper_cpu_compute_type
        try:
            self.model = WhisperModel(
                settings.whisper_model,
                device=device,
                compute_type=compute,
                cpu_threads=settings.cpu_threads,
                num_workers=1,
                download_root=str(settings.models_dir),
            )
        except Exception as e:
            if device != "cuda" or want == "cuda":
                raise
            log.warning("CUDA model load failed (%s); falling back to CPU", e)
            device, compute = "cpu", settings.whisper_cpu_compute_type
            self.model = WhisperModel(
                settings.whisper_model,
                device=device,
                compute_type=compute,
                cpu_threads=settings.cpu_threads,
                download_root=str(settings.models_dir),
            )
        self.device = device
        self.compute_type = compute
        log.info("loaded %s on %s (%s)", settings.whisper_model, device, compute)

    def transcribe(self, path: str, on_progress=None, clip_end: float | None = None) -> list[Word]:
        """Transcribe one audio file. on_progress(seconds_done) is called as
        segments arrive. clip_end stops transcription at that many seconds."""
        language = self.s.whisper_language or None
        if self.s.whisper_model.endswith(".en"):
            language = "en"
        kwargs = dict(
            language=language,
            beam_size=self.s.whisper_beam_size,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=self.s.whisper_vad,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        segments, _info = self.model.transcribe(path, **kwargs)
        words: list[Word] = []
        for seg in segments:
            if clip_end and seg.start >= clip_end:
                break  # segments are produced lazily, so this stops the work
            for w in seg.words or []:
                words.append(Word(w.word.strip(), float(w.start), float(w.end), float(w.probability)))
            if on_progress:
                on_progress(float(seg.end))
        return words
