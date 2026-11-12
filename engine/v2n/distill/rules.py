"""Rule-based distillation: deterministic, offline, no model required.

Same role as the ``fake`` ASR backend: it is a first-class citizen, not a stub,
so the entire distillation contract (spans, kinds, scores, gate) can be tested
in CI without a network call or a model download.

The rules only *remove* discourse scaffolding and *classify*; they never rewrite
content. Compression therefore stays high — see :func:`v2n.distill.ir.build_ir`,
where it is reported but never used as a hard gate.

Language handling
-----------------
The rules are language-aware because they were first written for Chinese and
silently failed on English lectures: every claim fell through to ``fact`` and
whisper's mid-sentence segments produced fragments such as "and specifically".
Script is detected from the text itself (CJK ratio); no configuration is needed
and mixed-language transcripts pick the dominant script.
"""
from __future__ import annotations

import re

_CJK = re.compile(r"[\u4e00-\u9fff]")

# Leading discourse markers carry no information. Longest alternatives first:
# Python's alternation is first-match, so ``我们`` would otherwise eat ``我们先看``.
_FILLER_CJK = re.compile(
    r"^(?:我们先看|我们先|我们|我先|这一节|这节课|这一讲|本节|本讲|接下来|下面|然后|"
    r"接着|首先|那么|所以|最后|强调|介绍|来说|讲讲)[，,、:：\s]*"
)

# English equivalents. Only phrases that carry no proposition are listed: cutting
# "in this section of the course" leaves a usable clause, cutting "we're going to"
# leaves "be covering...", so the latter is deliberately not stripped.
_FILLER_LATIN = re.compile(
    r"^(?:in this section(?: of the course)?|in this video|in this lesson|"
    r"in this lecture|so|now|okay|ok|alright|all right|well|and|but|basically|"
    r"essentially|right|next|then|first of all)[,;:\s]*",
    re.IGNORECASE,
)

_TRAILING_CJK = "。．.；;，, \t\u3000"
_TRAILING_LATIN = ".,;: \t"

_KEYWORDS_CJK: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("def", ("是指", "称为", "定义为", "叫做", "指的是", "即")),
    # Only prohibitions and explicit scope limits count as constraints. Weak
    # words such as 风险 or 注意 appear inside plain factual sentences and were
    # pulling facts into the cheat sheet.
    ("caution", ("不要", "避免", "禁止", "不得", "只做", "只读", "仅限于")),
    ("conclusion", ("总之", "综上", "总结", "因此")),
    ("principle", ("关键在于", "原理", "因为", "本质", "核心", "取决于")),
    ("step", ("先", "再", "然后", "接着", "步骤", "下一步")),
)

# Matched against a lowercased, space-padded string so "is" cannot fire inside
# "this" — the failure mode that makes naive keyword classifiers useless.
_KEYWORDS_LATIN: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("def", (" is defined as ", " refers to ", " is called ", " means ",
             " is known as ", " stands for ", " is short for ", " known as ",
             " also called ")),
    ("caution", (" must not ", " should not ", " never ", " avoid ",
                 " be careful ", " make sure ", " be sure ", " you must ",
                 " requires ", " do not ", " cannot ")),
    ("conclusion", (" in summary ", " to summarize ", " in conclusion ",
                    " to sum up ")),
    ("principle", (" because ", " since ", " so that ", " the reason ",
                   " depends on ", " in order to ", " which means ")),
    ("step", (" first ", " second ", " third ", " next ", " then ", " finally ",
              " step ", " to do this ", " after that ")),
)

# Kinds that carry enough decision value to earn a dedicated card / cheat line.
HIGH_VALUE = ("def", "principle", "step", "caution")

# Splitting a sentence moves its marker into a sibling clause: "只做只读验证，
# 不修改目标数据" leaves the second clause with no signal of its own. These two
# kinds are strong enough to be inherited by otherwise unlabelled siblings;
# step/principle are not, because "先" or "因为" in one clause says little about
# the next.
INHERITABLE = ("caution", "def")

MIN_CLAUSE_CHARS = 6
MIN_CLAUSE_WORDS = 4
MIN_LATIN_CHARS = 16
MAX_CLAIM_CHARS = 180


def detect_language(text: str) -> str:
    """``cjk`` when the text is predominantly Chinese characters, else ``latin``.

    Cheap and stable: a 20% CJK ratio already means the sentence is Chinese, and
    technical English with occasional Chinese terms still routes to ``latin``.
    """
    body = [ch for ch in text if not ch.isspace()]
    if not body:
        return "cjk"
    cjk = len(_CJK.findall(text))
    return "cjk" if cjk / len(body) >= 0.2 else "latin"


def normalise(text: str, lang: str | None = None) -> str:
    """Strip leading scaffolding and trailing punctuation. Never paraphrases."""
    lang = lang or detect_language(text)
    filler, trailing = ((_FILLER_CJK, _TRAILING_CJK) if lang == "cjk"
                        else (_FILLER_LATIN, _TRAILING_LATIN))
    out = text.strip()
    for _ in range(4):  # stacked markers, e.g. "so, now, ..."
        stripped = filler.sub("", out, count=1).rstrip(trailing)
        if stripped == out or not stripped:
            break
        out = stripped
    return out


def classify(text: str, lang: str | None = None) -> str:
    """Label the claim by interaction intent. First matching rule wins."""
    lang = lang or detect_language(text)
    if lang == "cjk":
        for kind, markers in _KEYWORDS_CJK:
            if any(m in text for m in markers):
                return kind
        return "fact"
    padded = f" {text.lower()} "
    for kind, markers in _KEYWORDS_LATIN:
        if any(m in padded for m in markers):
            return kind
    return "fact"


def score(text: str, kind: str) -> float:
    """Deterministic 0-1 salience. No randomness, no clock, no model."""
    length = len(text)
    value = 0.45
    value += 0.2 * min(1.0, length / 40.0)
    value += 0.1 if any(ch.isdigit() for ch in text) else 0.0
    value += 0.1 if kind in HIGH_VALUE else 0.0
    return round(min(value, 0.98), 3)


def is_informative(text: str, *, min_chars: int, lang: str | None = None) -> bool:
    """Reject pure filler: too short, repeated characters, or too few words."""
    body = text.strip()
    lang = lang or detect_language(body)
    if lang == "cjk":
        if len(body) < min_chars:
            return False
    else:
        # A character count is the wrong test for English: "and specifically"
        # passes a 6-char check while carrying no assertion.
        if len(body) < max(min_chars, MIN_LATIN_CHARS) or len(body.split()) < MIN_CLAUSE_WORDS:
            return False
    return len(set(body)) >= max(3, len(body) // 4)


_CLAUSE_SEPS_CJK = "，,；;：:"
_CLAUSE_SEPS_LATIN = ",;:"
_CONJUNCTIONS = ("以及", "并且", "而且", "但是", "而是", "同时", "其次", "另外",
                 "也", "并", "再")

# Chinese ASR frequently returns a long run with no punctuation at all, so
# punctuation-based splitting finds nothing and the claim stays 400 characters
# of raw speech. These discourse markers are where a speaker actually starts a
# new idea, so they act as a fallback boundary.
_CJK_BREAKS = ("那接下来", "那我们", "然后", "那么", "所以", "但是", "接下来",
               "比如说", "其实", "OK吧", "对吧")

MAX_CLAIM_CJK = 60


def _strip_conjunction(text: str, *, min_chars: int) -> str:
    """Drop a leading connective so a clause reads as a standalone claim."""
    for word in _CONJUNCTIONS:
        if text.startswith(word) and len(text) - len(word) >= min_chars:
            return text[len(word):].strip()
    return text


def split_clauses(text: str, *, min_chars: int = MIN_CLAUSE_CHARS,
                  lang: str | None = None,
                  max_chars: int = MAX_CLAIM_CHARS) -> list[str]:
    """Split a sentence at clause boundaries into atomic assertions.

    A whole sentence such as "只做只读验证，不修改目标数据，也不批量遍历记录"
    contains three separate rules; keeping it as one claim makes the outline read
    like the transcript. Fragments below the language's minimum are merged back
    into the previous clause instead of being emitted ("and specifically").

    Punctuation alone is not enough: ASR often emits a 290-character run with no
    comma at all. Oversized clauses are therefore split again at the nearest
    conjunction, which keeps them readable without rewriting anything.

    Every returned clause is a substring of ``text``, so traceability is
    preserved by construction.
    """
    lang = lang or detect_language(text)
    separators = _CLAUSE_SEPS_CJK if lang == "cjk" else _CLAUSE_SEPS_LATIN
    raw: list[str] = []
    buffer = ""
    for char in text:
        if char in separators:
            raw.append(buffer)
            buffer = ""
        else:
            buffer += char
    raw.append(buffer)

    def too_short(piece: str) -> bool:
        if lang == "cjk":
            return len(piece) < min_chars
        return len(piece.split()) < MIN_CLAUSE_WORDS

    out: list[str] = []
    for part in raw:
        piece = part.strip()
        if lang == "cjk":
            piece = _strip_conjunction(piece, min_chars=min_chars)
        if not piece:
            continue
        if too_short(piece) and out:
            joiner = "，" if lang == "cjk" else ", "
            out[-1] = f"{out[-1]}{joiner}{piece}"
            continue
        out.append(piece)

    expanded: list[str] = []
    for piece in out:
        if lang == "cjk" and len(piece) > MAX_CLAIM_CJK:
            expanded.extend(_break_cjk(piece, min_chars=min_chars))
        else:
            expanded.extend(_break_long(piece, lang=lang, max_chars=max_chars))
    return expanded or [text.strip()]


def _break_cjk(piece: str, *, min_chars: int) -> list[str]:
    """Split an unpunctuated Chinese run at the speaker's own discourse markers."""
    cuts: list[int] = []
    for marker in _CJK_BREAKS:
        start = piece.find(marker)
        while start != -1:
            if start >= min_chars and len(piece) - start >= min_chars:
                cuts.append(start)
            start = piece.find(marker, start + 1)
    if not cuts:
        return [piece]
    out: list[str] = []
    prev = 0
    for cut in sorted(set(cuts)):
        if cut - prev >= min_chars:
            out.append(piece[prev:cut].strip())
            prev = cut
    tail = piece[prev:].strip()
    if tail:
        out.append(tail)
    return [part for part in out if part] or [piece]


_LATIN_BREAKS = (" and ", " but ", " so ", " which ", " that ", " because ")


def _break_long(piece: str, *, lang: str, max_chars: int) -> list[str]:
    """Split an overlong clause at a conjunction near the midpoint.

    Keeps every fragment a substring of the original, so the claim stays
    traceable; a clause with no usable conjunction is left alone rather than cut
    mid-word.
    """
    if len(piece) <= max_chars:
        return [piece]
    if lang == "cjk":
        return [piece]
    midpoint = len(piece) // 2
    best: int | None = None
    for marker in _LATIN_BREAKS:
        start = 0
        while (pos := piece.find(marker, start)) != -1:
            head_words = len(piece[:pos].split())
            tail_words = len(piece[pos + len(marker):].split())
            if (head_words >= MIN_CLAUSE_WORDS and tail_words >= MIN_CLAUSE_WORDS
                    and (best is None or abs(pos - midpoint) < abs(best - midpoint))):
                best = pos
            start = pos + 1
    if best is None:
        return [piece]
    marker_len = next(len(m) for m in _LATIN_BREAKS if piece.startswith(m, best))
    head = piece[:best].strip()
    tail = piece[best + marker_len:].strip()
    return _break_long(head, lang=lang, max_chars=max_chars) + _break_long(
        tail, lang=lang, max_chars=max_chars)
