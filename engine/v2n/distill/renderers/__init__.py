"""Renderer registry.

A renderer is a pure function ``(ir, out_dir, cfg) -> Path``. It reads only the
IR, so an output format can be added or re-run without touching transcription,
distillation or the gate.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from ...config import Config
from ..model import DistillIR
from . import cheatsheet, outline, timeline

Renderer = Callable[[DistillIR, Path, Config], Path]

# Insertion order defines the output order for ``--outputs all``. Keep it stable:
# determinism across machines is a project invariant.
RENDERERS: dict[str, Renderer] = {
    "outline": outline.render,
    "timeline": timeline.render,
    "cheatsheet": cheatsheet.render,
}


def available() -> list[str]:
    return list(RENDERERS)


def resolve(names: Iterable[str] | None) -> list[str]:
    """Expand ``all`` and validate names. Unknown names are a hard error.

    Silently ignoring a typo would let a user believe cards were generated when
    they were not.
    """
    requested = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not requested:
        requested = ["outline"]
    expanded: list[str] = []
    for name in requested:
        chosen = list(RENDERERS) if name == "all" else [name]
        for item in chosen:
            if item not in RENDERERS:
                raise ValueError(
                    f"unknown distill output: {item!r}; "
                    f"available: {', '.join(available())} (or 'all')")
            if item not in expanded:
                expanded.append(item)
    return expanded


def render_all(ir: DistillIR, out_dir: Path, cfg: Config,
               names: Iterable[str] | None = None) -> list[tuple[str, Path]]:
    """Render the requested formats in the order the caller asked for."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[tuple[str, Path]] = []
    for name in resolve(names if names is not None else cfg.distill_outputs):
        produced.append((name, RENDERERS[name](ir, out_dir, cfg)))
    return produced
