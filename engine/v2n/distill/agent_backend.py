"""Distiller that reads answers written by an agent host.

The engine cannot call the host — a tool call is synchronous, so the host is
the caller, not a service the engine can dial. This backend therefore never
talks to a model: it assembles the IR from chunk answers the host has already
submitted through :mod:`v2n.agent`.

Everything downstream is shared. Traceability, deduplication, topic grouping and
the gate judge this IR exactly as they judge ``rules`` or ``llm`` output, which
is the point: swapping in a different source of intelligence must not lower the
quality bar.
"""
from __future__ import annotations

from pathlib import Path

from ..asr import Segment
from ..config import Config
from .model import DistillIR


class AgentDistiller:
    """Assemble the IR from work orders answered by an MCP host."""

    name = "agent"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # The bridge stores work orders next to the lesson artefacts; the
        # pipeline always loads Config with work_dir set to that directory.
        self.lesson_dir = Path(cfg.work_dir)

    def distil(self, segments: list[Segment], cfg: Config, *,
               lesson_key: str) -> DistillIR:
        from ..agent import build_ir, prepare, status

        try:
            state = status(self.lesson_dir)
        except FileNotFoundError:
            # Nothing prepared yet: create the work orders and say exactly what
            # to do next. Failing without that hint is the difference between a
            # five-second fix and a bug report.
            prepared = prepare(self.lesson_dir, segments, lesson_key=lesson_key)
            raise RuntimeError(
                f"prepared {prepared['chunks']} work order(s) under "
                f"{self.lesson_dir / '_agent'}; answer them via `v2n agent submit` "
                "or the v2n_submit tool, then run again to build the IR") from None

        if state["pending"]:
            head = ", ".join(state["pending"][:5])
            more = "" if len(state["pending"]) <= 5 else ", …"
            raise RuntimeError(
                f"{len(state['pending'])} of {state['chunks']} chunk(s) still "
                f"pending ({head}{more}); refusing to build an outline from an "
                "incomplete lesson")

        return build_ir(self.lesson_dir, segments, cfg)
