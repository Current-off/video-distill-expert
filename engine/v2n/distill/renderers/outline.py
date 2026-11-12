"""Outline renderer: topics and claims, one bullet per claim."""
from __future__ import annotations

from pathlib import Path

from ...asr import fmt_ts
from ...atomic import atomic_write_text
from ...config import Config
from ..model import KIND_LABELS, DistillIR
from ..readability import punctuate

FILENAME = "outline.md"

_PROVENANCE = {
    "rules": "> 由 `distill.json` 渲染，只删除口头语与重复，未改写原意。"
             "渲染层恢复了标点断句，不改任何字词。",
    # Claiming "not rewritten" for a model backend would be false.
    "llm": "> 由 `distill.json` 渲染；语句经模型压缩，每条均已回链校验，"
           "无法回链的命题已丢弃。渲染层恢复了标点断句，不改任何字词。",
}


def render(ir: DistillIR, out_dir: Path, _cfg: Config) -> Path:
    stats = ir.stats
    source = str(stats.get("source", "rules"))
    lines = [
        f"# {ir.lesson_key} · 提纲",
        "",
        _PROVENANCE.get(source, "> 由 `distill.json` 渲染。"),
        (f"> 主题 {stats.get('topics', 0)} · 命题 {stats.get('claims', 0)} · "
         f"回链覆盖 {float(stats.get('coverage', 0)) * 100:.0f}% · "
         f"压缩比 {float(stats.get('compression', 0)):.2f} · 后端 {source}"),
        "",
    ]
    if not ir.topics:
        lines += ["（无可提炼命题：转写为空或全部被判定为填充语。）", ""]
        return atomic_write_text(out_dir / FILENAME, "\n".join(lines))

    for index, topic in enumerate(ir.topics, 1):
        first = topic.claims[0] if topic.claims else None
        stamp = f" `{fmt_ts(first.start)}`" if first else ""
        lines.append(f"## {index}. {punctuate(topic.title, final=False)}{stamp}")
        lines.append("")
        for claim in topic.claims:
            label = KIND_LABELS.get(claim.kind, claim.kind)
            mark = "★ " if claim.emphasis else ""
            lines.append(f"- {mark}{punctuate(claim.text)} `{label}`")
        lines.append("")
    return atomic_write_text(out_dir / FILENAME, "\n".join(lines))
