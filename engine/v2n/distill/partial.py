"""Re-distil a slice of a lesson without paying to redo the whole thing.

Why this exists: a full lecture costs real tokens. When one topic comes out
wrong — a broken claim, a topic the model flattened — the user should pay to fix
that topic, not to re-transcribe and re-distil an hour of audio.

The operation is deliberately narrow:

* read the existing IR, keep every claim outside the window;
* re-run the configured backend on the segments inside the window only;
* remap the returned spans back to global segment indices, then rebuild.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..asr import Segment, load_transcript
from ..config import Config
from ..distill.backends import make_distiller
from ..distill.ir import content_segments, group_topics, write_ir
from ..distill.model import Claim, DistillIR
from ..distill.renderers import render_all
from ..distill.rules import detect_language
from ..transcript import TermTable, clean


def slice_segments(segments: list[Segment], start: float, end: float) -> tuple[list[Segment], int]:
    """Segments overlapping ``[start, end)`` plus the index offset of the first."""
    picked: list[Segment] = []
    offset = 0
    for index, segment in enumerate(segments):
        if segment.end <= start or segment.start >= end:
            continue
        if not picked:
            offset = index
        picked.append(segment)
    return picked, offset


def rebuild(ir: DistillIR, segments: list[Segment], cfg: Config, *,
            start: float, end: float) -> tuple[DistillIR, dict[str, Any]]:
    """Replace claims inside the window; leave everything outside untouched."""
    window, offset = slice_segments(segments, start, end)
    if not window:
        raise ValueError(f"no segment overlaps {start:.1f}s–{end:.1f}s")

    kept = [c for c in ir.claims if c.end <= start or c.start >= end]
    fresh_ir = make_distiller(cfg).distil(window, cfg, lesson_key=ir.lesson_key)
    # Spans are indices into ``window``; shift them back to lesson coordinates.
    shifted = [
        replace(claim, span=(claim.span[0] + offset, claim.span[1] + offset))
        for claim in fresh_ir.claims
    ]

    merged = sorted([*kept, *shifted], key=lambda c: (c.start, c.end))
    claims = [
        Claim(id=f"c{i + 1}", text=c.text, kind=c.kind, span=c.span,
              score=c.score, start=c.start, end=c.end)
        for i, c in enumerate(merged)
    ]
    lang = detect_language(" ".join(s.text for s in segments))
    topics = group_topics(claims, max_per_topic=cfg.distill_max_claims_per_topic,
                          lang=lang)

    total_chars = sum(len(s.text) for s in segments)
    kept_chars = sum(len(c.text) for c in claims)
    covered = {i for c in claims for i in range(c.span[0], c.span[1] + 1)}
    content = content_segments(segments, cfg, lang)
    stats = dict(ir.stats)
    stats.update({
        "claims": len(claims),
        "topics": len(topics),
        "coverage": (round(len(covered & content) / len(content), 4)
                     if content else 0.0),
        "compression": round(kept_chars / total_chars, 4) if total_chars else 0.0,
        "tokens_in": fresh_ir.stats.get("tokens_in", 0),
        "tokens_out": fresh_ir.stats.get("tokens_out", 0),
        "cost": fresh_ir.stats.get("cost"),
        "source": fresh_ir.stats.get("source", ir.stats.get("source")),
    })
    meta = {
        "window": [round(start, 3), round(end, 3)],
        "segments_in_window": len(window),
        "claims_replaced": len(ir.claims) - len(kept),
        "claims_added": len(shifted),
        "tokens_in": stats["tokens_in"],
        "tokens_out": stats["tokens_out"],
        "cost": stats["cost"],
        "source": stats["source"],
    }
    return DistillIR(lesson_key=ir.lesson_key, topics=topics, stats=stats), meta


def re_distil(lesson_dir: Path, cfg: Config, *, start: float, end: float,
              outputs: list[str] | None = None) -> dict[str, Any]:
    """Full round-trip on disk: read, rebuild, write, re-render."""
    from ..distill.ir import load_ir

    lesson_dir = Path(lesson_dir)
    ir = load_ir(lesson_dir / "distill.json")
    # Replaying the clean step must use the same term table as the original run,
    # otherwise ASR errors that were already corrected come back.
    terms = cfg.terms_file
    if terms is None and ir.stats.get("terms_file"):
        terms = Path(str(ir.stats["terms_file"]))
    segments = clean(load_transcript(lesson_dir / "transcript.txt"),
                     TermTable.load(terms), cfg).segments
    updated, meta = rebuild(ir, segments, cfg, start=start, end=end)
    if getattr(cfg, "hotkeys_enabled", False):
        # Imported here: hotkeys depends on distill, so a module-level import
        # would close the cycle.
        from ..hotkeys import PROJECT_FILE, load_hotkeys
        from ..hotkeys import apply as apply_hotkeys

        keys = load_hotkeys(project=lesson_dir / PROJECT_FILE,
                            user=cfg.hotkeys_user_file)
        if len(keys):
            updated = apply_hotkeys(updated, keys)
    write_ir(updated, lesson_dir / "distill.json")
    produced = render_all(updated, lesson_dir, cfg, outputs)
    meta["outputs"] = [name for name, _ in produced]
    return meta
