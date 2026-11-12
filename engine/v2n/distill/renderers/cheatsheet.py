"""Cheatsheet renderer: definitions, principles, steps and constraints only.

Sorting by kind rather than by time is the point of this format: a cheat sheet is
looked up, not read through.
"""
from __future__ import annotations

from pathlib import Path

from ...atomic import atomic_write_text
from ...config import Config
from ..model import KIND_LABELS, DistillIR
from ..readability import punctuate
from ..rules import HIGH_VALUE

FILENAME = "cheatsheet.md"
_ORDER = ("def", "principle", "step", "caution")


def render(ir: DistillIR, out_dir: Path, _cfg: Config) -> Path:
    lines = [f"# {ir.lesson_key} · 速查", ""]
    kept = 0
    # Hotkey hits first: these are the lines the speaker themselves flagged.
    marked = [c for c in ir.claims if c.emphasis]
    if marked:
        lines += ["## 重点", ""]
        lines += [f"- {punctuate(c.text)}" for c in marked]
        lines.append("")
        kept += len(marked)
    for kind in _ORDER:
        group = [c for c in ir.claims
                 if c.kind == kind and kind in HIGH_VALUE and not c.emphasis]
        if not group:
            continue
        lines += [f"## {KIND_LABELS.get(kind, kind)}", ""]
        for claim in group:
            lines.append(f"- {punctuate(claim.text)}")
        lines.append("")
        kept += len(group)
    if kept == 0:
        lines += ["（本次蒸馏没有定义、原理、步骤或约束类命题。）", ""]
    else:
        lines += [f"> 共 {kept} 条，按类型归并；完整上下文见 `notes.md`。", ""]
    return atomic_write_text(out_dir / FILENAME, "\n".join(lines))
