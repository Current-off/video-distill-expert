"""Integrity checks for resume safety.

The motivating bug: a 118-segment download died at segment 50 and left a
203 MB ``media.ts``. The next run saw ``media.ts exists`` and reused it, so the
transcript silently lost 58% of the lesson. No exception, no failed gate — the
output simply looked complete.

Two independent guards are provided:

1. :func:`playlist_duration` sums ``#EXTINF`` from the playlist (source of truth
   for how long the media *should* be).
2. :func:`wav_duration` reads the WAV header (how long the extracted audio
   *actually* is). Comparing the two detects truncated media without ffprobe.

:func:`expected_wav_bytes` is retained as a cheap size-only estimator for cases
where the WAV header cannot be read.
"""
from __future__ import annotations

import re
import wave
from dataclasses import dataclass
from pathlib import Path

# A minimal RIFF/WAVE header is 44 bytes, but encoders may add chunks: ffmpeg
# was measured at 78 bytes for the same PCM payload. This is therefore an
# *estimate* used only by the size-only check, whose 2% tolerance absorbs the
# difference. Prefer check_wav(): it reads the header and is exact regardless
# of chunk layout.
RIFF_HEADER_ESTIMATE = 44


def playlist_duration(text: str) -> float:
    """Sum all ``#EXTINF:<seconds>,`` declarations. Returns 0.0 if absent."""
    total = 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#EXTINF"):
            continue
        m = re.match(r"#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)\s*,", line)
        if m:
            total += float(m.group(1))
    return total


def expected_wav_bytes(duration_sec: float, *, sample_rate: int = 16000,
                       channels: int = 1, sample_width: int = 2) -> int:
    """Estimated PCM WAV size (header estimate + duration * rate * ch * width)."""
    return RIFF_HEADER_ESTIMATE + int(duration_sec * sample_rate * channels * sample_width)


def wav_duration(path: Path) -> float:
    """Actual duration read from a PCM WAV header (no ffmpeg needed)."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() == 0:
            raise ValueError(f"invalid WAV frame rate: {path}")
        return w.getnframes() / float(w.getframerate())


@dataclass(frozen=True)
class Verdict:
    ok: bool
    expected: float
    actual: float
    deviation: float          # (actual - expected) / expected
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "expected": round(self.expected, 3),
            "actual": round(self.actual, 3),
            "deviation": round(self.deviation, 5),
            "reason": self.reason,
        }


def check_wav(path: Path, expected: float, *, tolerance: float = 0.02) -> Verdict:
    """Compare actual WAV duration against an expected duration.

    ``expected <= 0`` means "unknown" and yields a skipped verdict (ok=True,
    reason='no_reference') — never a false failure on a valid file.
    """
    if expected <= 0:
        return Verdict(True, 0.0, 0.0, 0.0, "no_reference")
    if not path.exists():
        return Verdict(False, expected, 0.0, -1.0, "missing")
    try:
        actual = wav_duration(path)
    except (wave.Error, EOFError, ValueError, OSError) as exc:
        return Verdict(False, expected, 0.0, -1.0, f"unreadable:{type(exc).__name__}")
    dev = (actual - expected) / expected
    ok = abs(dev) <= tolerance
    reason = "ok" if ok else ("truncated" if dev < 0 else "longer_than_expected")
    return Verdict(ok, expected, actual, dev, reason)


def check_wav_size(path: Path, expected: float, *, tolerance: float = 0.02,
                   sample_rate: int = 16000, channels: int = 1,
                   sample_width: int = 2) -> Verdict:
    """Size-only fallback; an estimate, unlike the header-exact :func:`check_wav`.

    The ``actual`` field of the returned Verdict is an estimated duration
    derived from file size, not a measured one.
    """
    if expected <= 0:
        return Verdict(True, 0.0, 0.0, 0.0, "no_reference")
    if not path.exists():
        return Verdict(False, expected, 0.0, -1.0, "missing")
    exp_bytes = expected_wav_bytes(expected, sample_rate=sample_rate,
                                   channels=channels, sample_width=sample_width)
    actual = path.stat().st_size
    dev = (actual - exp_bytes) / exp_bytes
    ok = abs(dev) <= tolerance
    bytes_per_sec = sample_rate * channels * sample_width
    return Verdict(ok, expected, actual / bytes_per_sec, dev,
                   "ok" if ok else ("truncated" if dev < 0 else "larger_than_expected"))
