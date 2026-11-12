"""Apply hotkey hits to an IR: raise salience, flag emphasis, change nothing else."""
from __future__ import annotations

import dataclasses

from ..distill.model import Claim, DistillIR
from .model import HotkeySet

EMPHASIS_KINDS = ("emphasis", "caution")
MAX_BOOST = 0.30


def _topic_size(ir: DistillIR) -> int:
    """Reuse the original grouping size so topic structure survives a re-apply."""
    if not ir.topics:
        return 4
    return max(1, max(len(t.claims) for t in ir.topics))


def apply(ir: DistillIR, hotkeys: HotkeySet) -> DistillIR:
    """Return a new IR whose claims carry ``emphasis`` and ``hits``.

    Scoring is capped: three hotkeys in one sentence should not make it three
    times as important, and a speaker who says 「最重要的是」 constantly must not
    dominate their own outline.
    """
    if not len(hotkeys):
        return ir

    from ..distill.ir import group_topics

    updated: list[Claim] = []
    emphasised = 0
    for claim in ir.claims:
        hits = hotkeys.match(claim.text)
        if not hits:
            # A claim that no longer matches must lose the flag *and* the boost,
            # otherwise removing a hotkey would leave its effect behind.
            updated.append(dataclasses.replace(
                claim, score=claim.base_score or claim.score,
                emphasis=False, hits=()) if claim.hits or claim.emphasis else claim)
            continue
        boost = min(MAX_BOOST, sum(h.effective_weight for h in hits))
        marked = any(h.kind in EMPHASIS_KINDS for h in hits)
        emphasised += int(marked)
        base = claim.base_score or claim.score
        updated.append(dataclasses.replace(
            claim,
            base_score=base,
            score=round(min(0.99, base + boost), 3),
            emphasis=marked,
            hits=tuple(h.phrase for h in hits),
        ))

    stats = dict(ir.stats)
    stats["hotkeys"] = len(hotkeys)
    stats["emphasis_claims"] = emphasised
    return DistillIR(
        lesson_key=ir.lesson_key,
        topics=group_topics(updated, max_per_topic=_topic_size(ir),
                            lang=str(stats.get("language", "cjk"))),
        stats=stats,
        schema_version=ir.schema_version,
    )
