"""Settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .schedule import DEFAULT_WINDOW, Window, parse_window


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _cgroup_cpu_quota() -> int | None:
    """CPUs granted by a cgroup v2 quota (docker --cpus), or None when unlimited."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return None


def auto_cpu_threads() -> int:
    """Whisper threads when WHISPER_CPU_THREADS is 0 or "auto".

    With a CPU quota (docker --cpus N) use N. Otherwise use half the logical
    CPUs: about one thread per physical core on SMT machines (whisper gains
    little from hyperthreads) and headroom for the rest of the server on others.
    Capped at 16."""
    quota = _cgroup_cpu_quota()
    if quota:
        return min(16, quota)
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    return max(1, min(16, n // 2))


@dataclass
class Settings:
    abs_url: str = field(default_factory=lambda: os.environ.get("ABS_URL", "http://audiobookshelf:80").rstrip("/"))
    abs_api_key: str = field(default_factory=lambda: os.environ.get("ABS_API_KEY", ""))
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", ""))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "/data")))

    # false: no alignment worker or discovery (user data sync and stored results still served)
    alignment_enabled: bool = field(default_factory=lambda: _bool("ALIGNMENT_ENABLED", True))

    # transcription
    whisper_model: str = field(default_factory=lambda: os.environ.get("WHISPER_MODEL", "small.en"))
    whisper_device: str = field(default_factory=lambda: os.environ.get("WHISPER_DEVICE", "auto"))
    whisper_compute_type: str = field(default_factory=lambda: os.environ.get("WHISPER_COMPUTE_TYPE", "int8"))
    whisper_cpu_compute_type: str = field(default_factory=lambda: os.environ.get("WHISPER_CPU_COMPUTE_TYPE", "int8"))
    whisper_beam_size: int = field(default_factory=lambda: _int("WHISPER_BEAM_SIZE", 1))
    # 0 or "auto": see auto_cpu_threads
    whisper_cpu_threads: int = field(
        default_factory=lambda: 0 if os.environ.get("WHISPER_CPU_THREADS", "").strip().lower() == "auto"
        else _int("WHISPER_CPU_THREADS", 4))
    whisper_language: str = field(default_factory=lambda: os.environ.get("WHISPER_LANGUAGE", "en"))
    whisper_vad: bool = field(default_factory=lambda: _bool("WHISPER_VAD", True))

    # jobs
    max_audio_sec: float = field(default_factory=lambda: _float("MAX_AUDIO_SEC", 0.0))
    failed_retry_after_sec: float = field(default_factory=lambda: _float("FAILED_RETRY_AFTER_SEC", 86400.0))

    # auto mode
    auto_align: bool = field(default_factory=lambda: _bool("AUTO_ALIGN", True))
    auto_poll_minutes: float = field(default_factory=lambda: _float("AUTO_POLL_MINUTES", 10.0))
    auto_full_resync_hours: float = field(default_factory=lambda: _float("AUTO_FULL_RESYNC_HOURS", 6.0))
    auto_max_ebooks_per_audio: int = field(default_factory=lambda: _int("AUTO_MAX_EBOOKS_PER_AUDIO", 3))
    # automatic work only starts or resumes inside this local-time window ("" or "always": no window)
    auto_window: str = field(default_factory=lambda: os.environ.get("AUTO_WINDOW", DEFAULT_WINDOW))
    # an auto job still running at window end pauses at its next checkpoint; after this many
    # minutes without reaching one it is stopped anyway (it resumes from the last checkpoint)
    auto_pause_grace_minutes: float = field(default_factory=lambda: _float("AUTO_PAUSE_GRACE_MINUTES", 15.0))

    # API caches
    auth_cache_sec: float = field(default_factory=lambda: _float("AUTH_CACHE_SEC", 60.0))

    @property
    def window(self) -> Window | None:
        return parse_window(self.auto_window)

    @property
    def cpu_threads(self) -> int:
        """WHISPER_CPU_THREADS, or an automatic value when it is 0."""
        return self.whisper_cpu_threads if self.whisper_cpu_threads > 0 else auto_cpu_threads()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sync.db"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self):
        for d in (self.data_dir, self.results_dir, self.transcripts_dir, self.models_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings):
    global _settings
    _settings = s
