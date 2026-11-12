"""Timeline renderer: locate "what is said when" in a long recording."""
from __future__ import annotations

from pathlib import Path

from ...asr import fmt_ts
from ...atomic import atomic_write_text
from ...config import Config
from ..model import KIND_LABELS, DistillIR

FILENAME = "timeline.md"


def render(ir: DistillIR, out_dir: Path, _cfg: Config) -> Path:
    lines = [
        f"# {ir.lesson_key} · 时间轴",
        "",
        "> 每行对应一条可回链的命题，时间取自原转写分段。",
        "",
        "| 时间 | 类型 | 要点 |",
        "|---|---|---|",
    ]
    for claim in ir.claims:
        label = KIND_LABELS.get(claim.kind, claim.kind)
        text = claim.text.replace("|", "\\|")
        lines.append(f"| {fmt_ts(claim.start)} | {label} | {text} |")
    if not ir.claims:
        lines.append("| — | — | 无可提炼命题 |")
    lines.append("")
    return atomic_write_text(out_dir / FILENAME, "\n".join(lines))
