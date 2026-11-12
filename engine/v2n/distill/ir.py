"""Build and persist the distillation IR.

The invariant this module enforces: **a claim that cannot be traced back to the
transcript is dropped, never softened**. Transcription errors are "a word was
mis-heard"; distillation errors are "a conclusion the speaker never drew", and
the second kind is far more dangerous because it reads like the important part.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..asr import Segment
from ..atomic import atomic_write_text
from ..config import Config
from .model import SCHEMA_VERSION, Claim, DistillIR, Topic
from .rules import (
    INHERITABLE,
    classify,
    detect_language,
    is_informative,
    normalise,
    score,
    split_clauses,
)

OVERLAP_MIN = 0.5          # char-bigram overlap needed to accept a claim
DEDUPE_SIMILARITY = 0.85   # bigram Jaccard above which a claim is a repeat
TITLE_MAX = 18             # characters kept from the leading claim
TITLE_LATIN = 44           # English words are longer; cut on a word boundary


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def is_traceable(claim_text: str, source_text: str) -> bool:
    """True when the claim is a substring of, or heavily overlaps, its source.

    Substring is the normal path (rules strip scaffolding without rewriting).
    The overlap branch exists for future rewriting backends: it tolerates
    reordering while refusing genuinely new content.
    """
    if not claim_text:
        return False
    if claim_text in source_text:
        return True
    a, b = _bigrams(claim_text), _bigrams(source_text)
    if not a:
        return False
    return len(a & b) / len(a) >= OVERLAP_MIN


def _topic_title(claim: Claim, lang: str) -> str:
    """Head clause of the leading claim, never a mid-word cut.

    Truncating at a fixed character count cut clauses in half, producing titles
    that read like a rendering bug rather than a heading. Prefer the first
    clause; fall back to a soft cut only when there is no clause break.

    English needs a word-boundary cut: 18 characters lands mid-word
    ("Even mistyping a s…") because Latin words are longer than Chinese ones.
    """
    text = claim.text.strip()
    if lang == "latin":
        if len(text) <= TITLE_LATIN:
            return text
        cut = text[:TITLE_LATIN].rsplit(" ", 1)[0].rstrip(_PUNCT)
        return (cut or text[:TITLE_LATIN]) + "…"
    for sep in _CLAUSE_BREAKS:
        head, found, _ = text.partition(sep)
        if found and len(head.strip()) >= 4:
            return head.strip()
    if len(text) <= TITLE_MAX:
        return text
    return text[:TITLE_MAX].rstrip(_PUNCT) + "…"


_CLAUSE_BREAKS = ("，", ",", "；", ";", "：", ":", "。", ".")
_PUNCT = "，,、；;：:。. "


def group_topics(claims: list[Claim], *, max_per_topic: int,
                 lang: str = "cjk") -> list[Topic]:
    """Chunk consecutive claims into topics. Deterministic, no clustering."""
    topics: list[Topic] = []
    limit = max(1, max_per_topic)
    for start in range(0, len(claims), limit):
        chunk = claims[start:start + limit]
        if not chunk:
            continue
        topics.append(Topic(
            id=f"t{len(topics) + 1}",
            title=_topic_title(chunk[0], lang),
            span=(chunk[0].span[0], chunk[-1].span[1]),
            claims=tuple(chunk),
        ))
    return topics


def similarity(a: str, b: str) -> float:
    """Character-bigram Jaccard similarity. Used to drop repeated content."""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


_SENTENCE_END = ".!?。！？"
_TRAILING_QUOTES = "\"'”’）)】」"


@dataclass(frozen=True)
class _Unit:
    """One complete sentence, possibly assembled from several ASR segments."""

    text: str
    start_idx: int
    end_idx: int
    start: float
    end: float


def _ends_sentence(text: str) -> bool:
    body = text.rstrip().rstrip(_TRAILING_QUOTES).rstrip()
    return bool(body) and body[-1] in _SENTENCE_END


def merge_sentences(segments: list[Segment], lang: str, *,
                    pause: float = 0.6,
                    max_segments: int = 3, max_chars: int = 240,
                    soft_chars: int = 80) -> list[_Unit]:
    """Re-assemble sentences that the ASR split at a pause.

    Whisper cuts on silence, not on grammar: "we're going to be covering the
    different types of network" and "configurations that must be correct" arrive
    as separate segments. Distilling those separately yields fragments such as
    "and specifically".

    The primary boundary signal is the **silence between segments**, not
    punctuation: ``Segment.start/end`` already carry it, and Chinese ASR often
    emits no punctuation at all. A gap of ``pause`` seconds closes the unit, so
    the splitter no longer has to guess from character counts — which used to cut
    mid-word ("…诺贝尔尔奖" / "班的化学奖…").
    """
    joiner = "" if lang == "cjk" else " "
    units: list[_Unit] = []
    buffer: list[str] = []
    start_idx = 0
    start_time = 0.0

    for index, segment in enumerate(segments):
        text = segment.text.strip()
        if not text:
            continue
        if not buffer:
            start_idx, start_time = index, float(segment.start)
        buffer.append(text)
        candidate = joiner.join(buffer)
        following = segments[index + 1] if index + 1 < len(segments) else None
        gap = (following.start - segment.end) if following is not None else 0.0
        budget = soft_chars if lang == "cjk" else max_chars
        if (_ends_sentence(candidate) or gap >= pause
                or len(buffer) >= max_segments or len(candidate) >= budget):
            units.append(_Unit(candidate, start_idx, index, start_time, float(segment.end)))
            buffer = []
    if buffer:
        units.append(_Unit(joiner.join(buffer), start_idx, len(segments) - 1,
                           start_time, float(segments[-1].end)))
    return units


# Split after sentence punctuation. Latin text needs whitespace before the next
# sentence; Chinese typography has none, so a CJK character after the terminator
# is itself a valid boundary. Requiring ``\s+`` unconditionally made the splitter
# blind to every properly punctuated Chinese sentence.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])(?=\s+[^\d\s]|[\u4e00-\u9fff])")


def split_sentences(text: str) -> list[str]:
    """Split a merged unit back into sentences for per-sentence classification."""
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


def content_segments(segments: list[Segment], cfg: Config, lang: str) -> set[int]:
    """Segments that carry content once scaffolding is removed.

    Coverage is measured against these, not against every ASR segment: a segment
    that is pure discourse ("那接下来我们来学到", "so, now, okay") is legitimately
    dropped, and counting it as lost content would punish a correct distillation.
    """
    keep: set[int] = set()
    for index, segment in enumerate(segments):
        body = normalise(segment.text, lang)
        if is_informative(body, min_chars=cfg.distill_min_segment_chars, lang=lang):
            keep.add(index)
    return keep


def build_ir(segments: list[Segment], cfg: Config, *,
             lesson_key: str, source: str = "rules") -> DistillIR:
    """Distil cleaned segments into the IR, dropping untraceable claims.

    Pipeline: merge ASR fragments into sentences -> split into atomic clauses ->
    classify -> drop untraceable or repeated claims. One claim should carry one
    assertion, otherwise the outline is just the transcript with punctuation
    removed.
    """
    claims: list[Claim] = []
    kept_texts: list[str] = []
    covered: set[int] = set()
    skipped_short = 0
    untraceable = 0
    duplicates = 0

    lang = detect_language(" ".join(s.text for s in segments))
    units = merge_sentences(segments, lang, pause=cfg.distill_sentence_pause)

    for unit in units:
        produced = 0
        for sentence in split_sentences(unit.text):
            # Inheritance is a *sentence* concept. A merged unit can hold two or
            # three sentences, and "you must" in the first must not recolour the
            # third as a constraint — that produced "DHCP is the Dynamic Host
            # Control Protocol" labelled 约束.
            sentence_kind = classify(sentence, lang)
            for piece in split_clauses(normalise(sentence, lang),
                                       min_chars=cfg.distill_min_segment_chars,
                                       lang=lang):
                if not is_informative(piece, min_chars=cfg.distill_min_segment_chars,
                                      lang=lang):
                    skipped_short += 1
                    continue
                if not is_traceable(piece, unit.text):
                    untraceable += 1
                    continue
                if any(similarity(piece, prior) >= DEDUPE_SIMILARITY
                       for prior in kept_texts):
                    # A repeat still represents its segments: the content is
                    # present in the IR, just not twice.
                    duplicates += 1
                    produced += 1
                    continue
                kept_texts.append(piece)
                kind = classify(piece, lang)
                if kind == "fact" and sentence_kind in INHERITABLE:
                    kind = sentence_kind
                claims.append(Claim(id=f"c{len(claims) + 1}", text=piece, kind=kind,
                                    span=(unit.start_idx, unit.end_idx),
                                    score=score(piece, kind),
                                    start=unit.start, end=unit.end))
                produced += 1

        if not produced:
            # Every clause was filtered out. Dropping the unit would lose real
            # content — the exact failure this project was built to prevent — so
            # keep the sentence itself, at a low score.
            fallback = normalise(unit.text, lang)
            if (fallback and is_traceable(fallback, unit.text)
                    and not any(similarity(fallback, prior) >= DEDUPE_SIMILARITY
                                for prior in kept_texts)):
                kept_texts.append(fallback)
                kind = classify(unit.text, lang)
                claims.append(Claim(id=f"c{len(claims) + 1}", text=fallback, kind=kind,
                                    span=(unit.start_idx, unit.end_idx),
                                    score=score(fallback, kind) * 0.6,
                                    start=unit.start, end=unit.end))
                produced = 1
        if produced:
            covered.update(range(unit.start_idx, unit.end_idx + 1))

    topics = group_topics(claims, max_per_topic=cfg.distill_max_claims_per_topic,
                          lang=lang)
    total_chars = sum(len(s.text) for s in segments)
    kept_chars = sum(len(c.text) for c in claims)
    content = content_segments(segments, cfg, lang)
    covered_content = covered & content
    stats: dict[str, Any] = {
        "source": source,
        "language": lang,
        "segments": len(segments),
        "units": len(units),
        "content_segments": len(content),
        "claims": len(claims),
        "topics": len(topics),
        "skipped_short": skipped_short,
        "untraceable": untraceable,
        "duplicates": duplicates,
        "coverage": (round(len(covered_content) / len(content), 4) if content else 0.0),
        "compression": round(kept_chars / total_chars, 4) if total_chars else 0.0,
    }
    return DistillIR(lesson_key=lesson_key, topics=topics, stats=stats)


def write_ir(ir: DistillIR, path: Path) -> Path:
    """Stable serialisation: sorted keys, no clock, no locale dependence."""
    return atomic_write_text(
        path,
        json.dumps(ir.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def load_ir(path: Path) -> DistillIR:
    """Read a previously written IR so renderers can run without re-transcribing."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = int(data.get("schema_version", 0))
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported distill schema {version} in {path}")
    topics: list[Topic] = []
    for raw_topic in data.get("topics", []):
        claims = tuple(
            Claim(id=str(c["id"]), text=str(c["text"]), kind=str(c["kind"]),
                  span=(int(c["span"][0]), int(c["span"][1])),
                  score=float(c.get("score", 0.0)),
                  start=float((c.get("time") or [0.0, 0.0])[0]),
                  end=float((c.get("time") or [0.0, 0.0])[1]),
                  emphasis=bool(c.get("emphasis", False)),
                  hits=tuple(str(h) for h in (c.get("hits") or [])),
                  base_score=float(c.get("base", 0.0)) or 0.0)
            for c in raw_topic.get("claims", [])
        )
        topics.append(Topic(
            id=str(raw_topic["id"]), title=str(raw_topic["title"]),
            span=(int(raw_topic["span"][0]), int(raw_topic["span"][1])),
            claims=claims,
        ))
    return DistillIR(
        lesson_key=str(data.get("lesson_key", "")),
        topics=topics,
        stats=dict(data.get("stats", {})),
        schema_version=version,
    )
