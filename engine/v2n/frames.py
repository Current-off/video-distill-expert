"""Optional keyframe extraction with perceptual-hash deduplication.

Lecture slides often stay unchanged for minutes: a 65-minute screen-record can
produce 296 raw frames and only 29 distinct ones. Deduplication is therefore the
default when frames are enabled.

All optional imports are lazy. With ``frames_enabled=False`` (the default) this
module is never called, so a core-only install needs neither ffmpeg nor Pillow.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config
from .ingest import ffmpeg_bin


def extract_frames(media: Path, out_dir: Path, cfg: Config) -> list[Path]:
    """Uniform time sampling via ffmpeg; returns frame paths in time order."""
    frames = out_dir / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    for old in frames.glob("frame_*.jpg"):
        old.unlink()
    cmd = [
        ffmpeg_bin(), "-y", "-i", str(media),
        "-vf", f"fps=1/{max(cfg.frame_interval, 0.1):.6f}",
        "-q:v", "3", str(frames / "frame_%06d.jpg"),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return sorted(frames.glob("frame_*.jpg"))


def dhash(path: Path) -> int:
    """64-bit difference hash (8x8 comparisons on a 9x8 grayscale image)."""
    from PIL import Image
    with Image.open(path) as im:
        small = im.convert("L").resize((9, 8))
        px = list(small.getdata())
    value = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            value = (value << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return value


def dedupe(paths: list[Path], *, threshold: int = 8) -> list[Path]:
    """Keep a frame only if its dHash differs from the last kept one."""
    try:
        hashes = [(p, dhash(p)) for p in paths]
    except ImportError:
        # Pillow/numpy absent -> skip dedupe rather than fail the whole run.
        return paths
    if not hashes:
        return []
    kept: list[Path] = [hashes[0][0]]
    last = hashes[0][1]
    for p, h in hashes[1:]:
        if (h ^ last).bit_count() > threshold:
            kept.append(p)
            last = h
    return kept


def frame_for_time(frames: list[Path], seconds: float, cfg: Config) -> Path | None:
    """Best-effort mapping from a transcript timestamp to a frame file."""
    if not frames or cfg.frame_interval <= 0:
        return None
    idx = int(seconds // cfg.frame_interval)
    return frames[idx] if 0 <= idx < len(frames) else None


def collect_frames(media: Path, out_dir: Path, cfg: Config) -> tuple[list[Path], int]:
    if not cfg.frames_enabled:
        return [], 0
    raw = extract_frames(media, out_dir, cfg)
    kept = dedupe(raw, threshold=cfg.dedupe_threshold)
    return kept, len(raw) - len(kept)
