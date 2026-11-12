"""Deterministic transcript cleanup: term fixes, hallucination drop, re-segment.

Why a fixed dictionary rather than fuzzy matching or an LLM rewrite: ASR errors
for domain terms are *systematic* (the same tool name is mis-heard the same way
on every occurrence), and silently rewriting content is worse than leaving one
odd word. Only three defect classes are handled; nothing else is paraphrased.

Substitution order matters. Three rules, each fixing a real bug:

1. Replace longer keys first, or ``域名`` would consume ``紫域名``.
2. Mask already-correct terms before substituting, or a rule like
   ``EDUSR -> EDUSRC`` turns a correct ``EDUSRC`` into ``EDUSRCC``.
3. Identity pairs (``("FOFA", "FOFA")``) are skipped; they are protection
   entries, not replacements.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from .asr import Segment
from .config import Config

_MASK = "\x00"
_HALLU_RUN = re.compile(r"(.)\1{4,}")


@dataclass
class CleanResult:
    segments: list[Segment]
    fixes: int = 0
    dropped: int = 0
    merged: int = 0


class TermTable:
    """``[[wrong, right], ...]`` loaded from YAML; wrong==right means 'protect'."""

    def __init__(self, pairs: Iterable[tuple[str, str]] | None = None):
        self.pairs: list[tuple[str, str]] = []
        self.protect: list[str] = []
        # Where this table came from, so a later partial re-distil can reuse it
        # instead of replaying the clean step with no term correction at all.
        self.source: str | None = None
        for bad, good in pairs or []:
            bad, good = str(bad).strip(), str(good).strip()
            if not bad:
                continue
            if bad == good:
                self.protect.append(bad)
            else:
                self.pairs.append((bad, good))
        self.pairs.sort(key=lambda kv: -len(kv[0]))

    @classmethod
    def load(cls, path: Path | str | None) -> TermTable:
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"terms file not found: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        if isinstance(data, dict):
            data = data.get("terms") or data.get("fixes") or []
        table = cls([(row[0], row[1]) for row in data if len(row) >= 2])
        table.source = str(p)
        return table

    def apply(self, text: str) -> tuple[str, int]:
        """Mask-before, mask-after substitution.

        Both the *protected* words and each *replacement result* are replaced by
        opaque tokens first, then restored at the very end. Without masking the
        results, a short rule can re-match text produced by a longer rule:
        ``紫域名 -> 子域名`` followed by ``域名 -> 领域`` yields ``子领域``.
        Tokens use distinct prefixes (P/R) so restoration is unambiguous.
        """
        n = 0
        produced: list[str] = []
        for i, word in enumerate(self.protect):
            text = text.replace(word, f"{_MASK}P{i}{_MASK}")
        for bad, good in self.pairs:
            c = text.count(bad)
            if not c:
                continue
            token = f"{_MASK}R{len(produced)}{_MASK}"
            produced.append(good)
            text = text.replace(bad, token)
            n += c
        for j, word in enumerate(produced):
            text = text.replace(f"{_MASK}R{j}{_MASK}", word)
        for i, word in enumerate(self.protect):
            text = text.replace(f"{_MASK}P{i}{_MASK}", word)
        return text, n


def is_hallucination(text: str) -> bool:
    """A VAD-boundary artefact: one character repeated >=5 times with little
    other content, e.g. ``试试试试试试试试``. Drop the whole segment."""
    t = text.strip()
    if not t or not _HALLU_RUN.search(t):
        return False
    unique = len(set(t))
    return unique < max(2, len(t) // 3)


def merge_segments(segments: list[Segment], *, max_chars: int = 120,
                   max_gap: float | None = None) -> tuple[list[Segment], int]:
    """Join VAD fragments until a terminator, a length cap, or a long silence.

    ``max_gap`` matters more than it looks. Merging purely by character count
    destroys the **silence between segments**, which is the only sentence-boundary
    signal that survives Chinese ASR (measured: 0 of 400 segments carried any
    punctuation). Distillation downstream then has to guess boundaries from
    character budgets and cuts mid-word. Preserving the gap keeps that information
    alive until it is needed.
    """
    if not segments:
        return [], 0
    out: list[Segment] = []
    buf: Segment | None = None
    terminators = "。！？!?；;"

    def flush() -> None:
        nonlocal buf
        if buf is not None:
            out.append(buf)
            buf = None

    for s in segments:
        text = s.text.strip()
        if not text:
            continue
        pause = (s.start - buf.end) if buf is not None else 0.0
        too_far = max_gap is not None and pause >= max_gap
        if buf is None:
            buf = Segment(s.start, s.end, text)
        elif len(buf.text) + len(text) <= max_chars and not too_far:
            buf = Segment(buf.start, s.end, buf.text + text)
        else:
            flush()
            buf = Segment(s.start, s.end, text)
        if buf.text and buf.text[-1] in terminators:
            flush()
    flush()
    return out, len(segments) - len(out)


def clean(segments: list[Segment], table: TermTable, cfg: Config) -> CleanResult:
    kept: list[Segment] = []
    fixes = 0
    dropped = 0
    for s in segments:
        if cfg.drop_hallucinations and is_hallucination(s.text):
            dropped += 1
            continue
        text, n = table.apply(s.text)
        fixes += n
        kept.append(Segment(s.start, s.end, text))
    if cfg.merge_short_segments:
        kept, merged = merge_segments(kept, max_chars=cfg.merge_max_chars,
                                      max_gap=cfg.clean_merge_max_gap)
    else:
        merged = 0
    return CleanResult(kept, fixes, dropped, merged)
