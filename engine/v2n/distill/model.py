"""Distillation data model.

One intermediate representation (IR), many renderers. The IR is the *only*
place where quality is judged: every claim carries a ``span`` back to the
cleaned transcript, so a renderer can never invent content that the transcript
does not contain. Adding a new output format means adding a renderer, never a
second distillation path — otherwise six formats would drift into six different
quality standards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Claim kinds. These describe *interaction intent*, not subject matter, which is
# what keeps distillation usable across courses.
KINDS = ("fact", "def", "principle", "step", "caution", "conclusion")

KIND_LABELS = {
    "fact": "事实",
    "def": "定义",
    "principle": "原理",
    "step": "步骤",
    "caution": "约束",
    "conclusion": "结论",
}


@dataclass(frozen=True)
class Claim:
    """A single assertion that can be traced back to transcript segments."""

    id: str
    text: str
    kind: str
    span: tuple[int, int]
    score: float
    start: float = 0.0
    end: float = 0.0
    # Hotkey hits: which trigger phrases fired, and whether they mark the claim
    # as something the speaker emphasised. Additive fields — older IR files load
    # with both defaulted.
    emphasis: bool = False
    hits: tuple[str, ...] = ()
    # Score before any hotkey boost. Kept so re-applying hotkeys is idempotent:
    # without it, every render would add the boost again and the order would
    # drift on each refresh.
    base_score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind,
            "span": [int(self.span[0]), int(self.span[1])],
            "score": round(float(self.score), 3),
            "time": [round(float(self.start), 3), round(float(self.end), 3)],
            "emphasis": bool(self.emphasis),
            "hits": list(self.hits),
            "base": round(float(self.base_score), 3),
        }


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    span: tuple[int, int]
    claims: tuple[Claim, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "span": [int(self.span[0]), int(self.span[1])],
            "claims": [c.as_dict() for c in self.claims],
        }


@dataclass
class DistillIR:
    lesson_key: str
    topics: list[Topic] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @property
    def claims(self) -> list[Claim]:
        return [c for t in self.topics for c in t.claims]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lesson_key": self.lesson_key,
            "stats": self.stats,
            "topics": [t.as_dict() for t in self.topics],
        }
