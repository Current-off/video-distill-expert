"""Hotkeys: phrases that mark what the speaker considers important.

Three tiers, merged by precedence:

* ``builtin`` — ships with the package (「最重要的是」「关键在于」…); generic speech
  markers, never course-specific jargon;
* ``project`` — written per lesson, usually by the model, after it has seen the
  transcript (so it can notice this speaker's own habits and this subject's terms);
* ``user`` — hand-written, overrides everything.

A hit raises a claim's salience and flags it for the renderers; it never invents
content, which is what keeps this feature compatible with the traceability rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field

KINDS = ("emphasis", "caution", "structure", "definition")

# How much one hit may raise a claim's score. Deliberately modest: a phrase is a
# hint about importance, not proof of it, and a lecture that says 「最重要」five
# times must not outrank a lecture with one real insight.
WEIGHTS = {"emphasis": 0.18, "caution": 0.12, "definition": 0.08, "structure": 0.04}


@dataclass(frozen=True)
class Hotkey:
    phrase: str
    kind: str = "emphasis"
    weight: float | None = None
    source: str = "builtin"

    @property
    def effective_weight(self) -> float:
        return self.weight if self.weight is not None else WEIGHTS.get(self.kind, 0.1)

    def as_dict(self) -> dict[str, object]:
        return {"phrase": self.phrase, "kind": self.kind,
                "weight": round(self.effective_weight, 3), "source": self.source}


@dataclass
class HotkeySet:
    items: list[Hotkey] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def match(self, text: str) -> list[Hotkey]:
        """All hotkeys occurring in ``text``. Longest phrase first, no overlaps."""
        lowered = text.lower()
        found: list[Hotkey] = []
        taken: list[tuple[int, int]] = []
        for hotkey in sorted(self.items, key=lambda h: -len(h.phrase)):
            needle = hotkey.phrase.lower()
            start = lowered.find(needle)
            while start != -1:
                end = start + len(needle)
                if not any(s < end and start < e for s, e in taken):
                    found.append(hotkey)
                    taken.append((start, end))
                start = lowered.find(needle, start + 1)
        return found


def merge(*sets: HotkeySet) -> HotkeySet:
    """Merge in order; a later tier overrides an earlier one with the same phrase."""
    by_phrase: dict[str, Hotkey] = {}
    for hotkeys in sets:
        for hotkey in hotkeys:
            by_phrase[hotkey.phrase] = hotkey
    ordered = sorted(by_phrase.values(), key=lambda h: (-h.effective_weight, h.phrase))
    return HotkeySet(ordered)
