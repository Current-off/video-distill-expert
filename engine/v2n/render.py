"""Deterministic Markdown rendering. No timestamps of "now" are written, so the
same input always produces byte-identical output (useful for CI snapshots).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .asr import Segment, fmt_ts
from .atomic import atomic_write_text
from .config import Config


def render_transcript_clean(segments: list[Segment], path: Path) -> None:
    lines = ["# 清洗版转写", "",
             "> 仅做术语纠正、幻觉段丢弃与断句合并；不改写、不补写内容。", ""]
    for s in segments:
        lines.append(f"**[{fmt_ts(s.start)}-{fmt_ts(s.end)}]** {s.text}")
        lines.append("")
    atomic_write_text(path, "\n".join(lines))


def render_notes(meta: dict[str, Any], segments: list[Segment],
                 frames: list[Path], path: Path, cfg: Config) -> None:
    v = meta.get("integrity") or {}
    lines = [
        f"# {meta.get('lesson_key', 'lesson')} · 学习笔记",
        "",
        "## 运行信息",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| ASR 后端 | `{meta.get('backend', 'fake')}` |",
        f"| 媒体时长 | {float(meta.get('expected_duration') or 0):.1f} s |",
        f"| WAV 实际时长 | {float(v.get('actual') or 0):.1f} s |",
        (f"| 完整性 | {'PASS' if v.get('ok') else 'FAIL'} "
         f"(偏差 {(float(v.get('deviation') or 0) * 100):+.2f}%，{v.get('reason', '')}) |"),
        f"| 原始段数 | {meta.get('raw_segments', 0)} |",
        f"| 清洗后段数 | {len(segments)} |",
        f"| 术语纠正 | {meta.get('fixes', 0)} 处 |",
        f"| 丢弃幻觉段 | {meta.get('dropped', 0)} |",
        f"| 合并碎段 | {meta.get('merged', 0)} |",
        (f"| 关键帧 | {len(frames)} "
         f"(去重丢弃 {meta.get('frames_dropped', 0)}) |"),
        f"| 门禁 | {'PASS' if meta.get('gate_passed') else 'FAIL'} |",
        "",
        "## 正文",
        "",
    ]
    for i, s in enumerate(segments, 1):
        lines.append(f"### {i}. [{fmt_ts(s.start)}-{fmt_ts(s.end)}]")
        lines.append("")
        lines.append(s.text)
        lines.append("")
        if frames and cfg.frame_interval > 0:
            idx = int(s.start // cfg.frame_interval)
            if 0 <= idx < len(frames):
                lines.append(f"![frame](frames/{frames[idx].name})")
                lines.append("")
    if meta.get("gate_reasons"):
        lines += ["## 门禁明细", ""]
        lines += [f"- {r}" for r in meta["gate_reasons"]]
        lines.append("")
    lines += [
        "## 复现",
        "",
        "```bash",
        (f"v2n run --source <your-media> --out <out-dir> "
         f"--asr {cfg.asr_backend}"),
        "```",
        "",
        "> 生成物（媒体、转写、笔记）默认被 `.gitignore` 排除，不要提交派生内容。",
        "",
    ]
    atomic_write_text(path, "\n".join(lines))


def render_summary(rows: list[dict[str, Any]], path: Path) -> None:
    lines = ["# Batch summary", "", "| key | exit | seconds | gate |", "|---|---:|---:|---|"]
    for r in rows:
        lines.append(f"| {r.get('key')} | {r.get('exit')} | "
                     f"{r.get('seconds')} | {r.get('gate')} |")
    atomic_write_text(path, "\n".join(lines) + "\n")


def write_run_summary(meta: dict[str, Any], path: Path) -> None:
    atomic_write_text(path, json.dumps(meta, ensure_ascii=False, indent=2,
                                   sort_keys=True))
