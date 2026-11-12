"""Display-layer punctuation restoration for run-on CJK claim text.

Whisper-small on Chinese emits unpunctuated speech, so a verbatim claim reads
like "它是一个开源轻量级的工具不需要你熟悉复杂的操作". The IR keeps that
text verbatim on purpose — traceability checks characters, not readability —
so this module runs at render time only: it inserts punctuation and never
adds, removes or reorders a single character. ``strip(punctuate(x)) ==
strip(x)`` holds by construction and is pinned by test.

Deliberately conservative:

* Text that already contains punctuation is left alone — ASR with a bigger
  model punctuates on its own, and re-punctuating punctuated text is how
  renderers start "improving" content.
* Latin text is left alone; English ASR punctuates, and the marker list is
  Chinese discourse anyway.
* Commas land only at discourse markers ("所以", "然后", "比如说" …) with
  enough clause on both sides, so a marker hidden inside a word can still
  fire but will never cut a short phrase in half.
"""
from __future__ import annotations

import re

from .rules import detect_language

# Full-width and half-width punctuation this module may insert or respect.
_INSERTED = "，。"
_ANY_PUNCT = re.compile(r"[，,。.、；;：:！!?？]")

# Comma goes *before* the marker. Longest first: "然后就是" must win over
# "然后" and "就是", or the split lands one marker early.
_BREAK_BEFORE = (
    "然后就是", "也就是说", "换句话说", "比如说",
    "所以说", "说白了",
    "比如", "例如", "然后", "所以", "但是", "不过", "因为", "由于",
    "如果", "只要", "同时", "并且", "以及", "最后", "首先", "其次",
    "其实", "另外", "还有", "除了", "不仅", "因此", "当然", "接下来",
    "目前", "就是", "还是", "或者", "不需要", "那我们", "那么",
)

# Comma goes *after* the marker: these close a clause rather than open one.
_BREAK_AFTER = ("的话", "对吧", "对不对", "是不是", "也好")

# A clause needs this many characters on both sides of the break, otherwise
# the marker is part of a phrase ("还有" in "他还有个问题" is fine either
# way, but "因为" starting a two-character tail would read worse with a
# comma than without).
_MIN_HEAD, _MIN_TAIL = 4, 3


def punctuate(text: str, final: bool = True) -> str:
    """Return ``text`` with commas at clause boundaries and a closing period.

    ``final=False`` is for headings: the same commas, no trailing period.
    Anything the rules cannot safely classify is returned unchanged — a
    missing comma is a cosmetic loss, a wrong one looks like rewriting.
    """
    text = text.strip()
    if not text or _ANY_PUNCT.search(text) or detect_language(text) != "cjk":
        return text
    out = _insert_breaks(_insert_echo(text))
    if final and not out.endswith(tuple(_INSERTED + "!？!?")):
        out += "。"
    return out


# A repeated 4-gram is a spoken echo ("专门展示给谁的专门展示给") only when the
# second copy follows the first within this many characters; further apart it
# is two independent mentions and the comma would cut a legitimate phrase.
_ECHO_GAP = 6


def _insert_echo(text: str) -> str:
    """Break before the second copy of a closely repeated 4-gram."""
    n = len(text)
    for i in range(4, n - 2):
        phrase = text[i:i + 4]
        j = text.find(phrase)
        if j < 0 or j >= i:
            continue
        if 0 < i - (j + 4) <= _ECHO_GAP and n - i >= _MIN_TAIL:
            return text[:i] + "，" + text[i:]
    return text


def strip_punct(text: str) -> str:
    """Remove the punctuation this layer may insert or honour.

    The round-trip check uses this: ``strip_punct(punctuate(x)) ==
    strip_punct(x)`` is the exact promise "no character was touched".
    """
    return _ANY_PUNCT.sub("", text)


def _insert_breaks(text: str) -> str:
    out: list[str] = []
    head = 0  # characters since the last break (or the start)
    i, n = 0, len(text)
    while i < n:
        step = _match(text, i, head, n)
        if step is None:
            out.append(text[i])
            head += 1
            i += 1
            continue
        _marker, before, length = step
        comma = "，"
        chunk = text[i:i + length]
        out.append((comma + chunk) if before else (chunk + comma))
        head = 0
        i += length
    return "".join(out)


def _match(text: str, i: int, head: int, n: int) -> tuple[str, bool, int] | None:
    """Marker starting at ``i``, or ``None``. Tries before-markers first."""
    if head >= _MIN_HEAD:
        for marker in _BREAK_BEFORE:
            length = len(marker)
            if text.startswith(marker, i) and n - i - length >= _MIN_TAIL:
                return marker, True, length
    for marker in _BREAK_AFTER:
        length = len(marker)
        if text.startswith(marker, i) and head >= 3 and n - i - length >= _MIN_TAIL:
            return marker, False, length
    return None
