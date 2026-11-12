"""Speech-to-text backends behind one small protocol.

``fake`` is a first-class backend, not a stub: it lets the whole pipeline run in
CI with no model download, no GPU and no network. ``whisper`` lazily imports
faster-whisper so the core package stays installable without CTranslate2.

Every real run writes ``asr_meta.json`` (backend, model, device, compute type,
language, prompt hash). Without those fields a later quality regression cannot
be attributed to a change of model or prompt.
"""
from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .atomic import atomic_write_text
from .config import Config


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


class ASRBackend(Protocol):
    name: str

    def transcribe(self, wav: Path, out_dir: Path) -> list[Segment]: ...


# Deliberately generic sentences. They contain two known ASR mis-hearings so the
# demo exercises the term-correction stage without shipping any real content.
_DEMO_LINES = [
    "这一节介绍子域名枚举的基本思路，以及为什么需要先确定资产范围。",
    "我们先看紫域名收集的常见方式，再讨论 SQL 注入的判定方法。",
    "Circle 注入的关键在于确认参数是否被拼接进查询语句。",
    "接着说明如何使用 NMAP 做端口探测，并解释半开扫描的原理。",
    "最后强调：只做只读验证，不修改目标数据，也不批量遍历记录。",
    "本节涉及护网行动中常见的资产梳理流程与风险分级方式。",
]


class FakeBackend:
    """Deterministic, offline transcript derived only from the WAV duration."""

    name = "fake"

    def __init__(self, *, segment_seconds: float = 10.0, lines: list[str] | None = None):
        self.segment_seconds = segment_seconds
        self.lines = list(lines or _DEMO_LINES)

    def transcribe(self, wav: Path, out_dir: Path) -> list[Segment]:
        with wave.open(str(wav), "rb") as w:
            duration = w.getnframes() / float(w.getframerate())
        n = (0 if duration <= 0 or self.segment_seconds <= 0
             else max(1, int(duration // self.segment_seconds)))
        segs: list[Segment] = []
        for i in range(n):
            start = i * self.segment_seconds
            end = min((i + 1) * self.segment_seconds, duration)
            segs.append(Segment(start, end, self.lines[i % len(self.lines)]))
        atomic_write_text(
            out_dir / "asr_meta.json",
            json.dumps({"backend": "fake", "duration": round(duration, 3),
                        "segments": len(segs), "lines": len(self.lines)},
                       ensure_ascii=False, indent=2))
        return segs


def _load_whisper(model_cls, cfg: Config, device: str, compute: str):
    """Load a cached model without any network round-trip; download only if absent.

    This package advertises itself as offline-first, but faster-whisper still
    checks the hub on every construction. Measured failure: the model was fully
    cached yet a dead system proxy returned HTTP 502 and killed the whole run
    before transcription started. Trying the local files first makes the promise
    true; falling back keeps the first-time download working.
    """
    kwargs = {"device": device, "compute_type": compute, "cpu_threads": cfg.asr_threads}
    try:
        return model_cls(cfg.asr_model, local_files_only=True, **kwargs)
    except Exception:
        # Any failure here can legitimately mean "not cached yet"; the retry
        # below is the only path that may touch the network.
        return model_cls(cfg.asr_model, **kwargs)


class WhisperBackend:
    """faster-whisper backend; imported only when selected."""
    name = "whisper"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "whisper backend needs the 'asr' extra: pip install -e '.[asr]'"
            ) from exc
        device = cfg.asr_device
        compute = cfg.asr_compute_type
        # faster-whisper expects a concrete device string; "auto" degrades to CPU
        # rather than being passed through as an invalid value.
        if device == "auto":
            device = "cpu"
        # CTranslate2 rejects float16-family compute types on CPU. A config
        # copied from a GPU host must degrade instead of failing at lesson 1.
        if device == "cpu" and compute in ("float16", "int8_float16",
                                           "bfloat16", "float32"):
            compute = "int8"
        if device == "cuda" and compute == "int8":
            compute = "int8_float16"
        self.configured_device = device
        self.configured_compute_type = compute
        self.model = _load_whisper(WhisperModel, cfg, device, compute)

    def transcribe(self, wav: Path, out_dir: Path) -> list[Segment]:
        cfg = self.cfg
        prompt = build_initial_prompt(cfg.hotwords)
        segments, info = self.model.transcribe(
            str(wav), language=cfg.asr_language, initial_prompt=prompt or None,
            vad_filter=True,
        )
        out = [Segment(float(s.start), float(s.end), s.text.strip())
               for s in segments if s.text.strip()]
        atomic_write_text(
            out_dir / "asr_meta.json",
            json.dumps({
                "backend": "whisper",
                "model": cfg.asr_model,
                "device": self.configured_device,
                "compute_type": self.configured_compute_type,
                "language": cfg.asr_language,
                "cpu_threads": cfg.asr_threads,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "prompt_chars": len(prompt),
                "duration": getattr(info, "duration", None),
                "segments": len(out),
            }, ensure_ascii=False, indent=2))
        return out


def build_initial_prompt(hotwords: list[str], *, limit: int = 36) -> str:
    """Bias the decoder *before* it mis-hears a term.

    Whisper's initial_prompt has a ~223-token budget; without a tokenizer here
    we conservatively cap the number of terms instead of claiming a token count.
    Longer terms should go first if you need to prioritise them.
    """
    seen: list[str] = []
    for w in hotwords:
        w = str(w).strip()
        if w and w not in seen:
            seen.append(w)
    picked = seen[:limit]
    if not picked:
        return ""
    return "本节涉及以下术语：" + "、".join(picked) + "。"


def make_backend(cfg: Config) -> ASRBackend:
    if cfg.asr_backend == "fake":
        return FakeBackend()
    if cfg.asr_backend == "whisper":
        return WhisperBackend(cfg)
    raise ValueError(f"unknown asr_backend: {cfg.asr_backend!r}")


def fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def save_transcript(segments: list[Segment], path: Path) -> None:
    """Plain ``[mm:ss-mm:ss] text`` format, one segment per line."""
    lines = [f"[{fmt_ts(s.start)}-{fmt_ts(s.end)}] {s.text}" for s in segments]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def load_transcript(path: Path) -> list[Segment]:
    import re
    out: list[Segment] = []
    pat = re.compile(r"^\[(\d+):(\d{2})(?::(\d{2}))?-(\d+):(\d{2})(?::(\d{2}))?\]\s*(.*)$")

    def to_sec(h: str, m: str, s: str | None) -> float:
        if s is None:
            return int(h) * 60 + int(m)
        return int(h) * 3600 + int(m) * 60 + int(s)

    for line in path.read_text(encoding="utf-8").splitlines():
        m = pat.match(line)
        if not m:
            continue
        start = to_sec(m.group(1), m.group(2), m.group(3))
        end = to_sec(m.group(4), m.group(5), m.group(6))
        out.append(Segment(start, end, m.group(7)))
    return out
