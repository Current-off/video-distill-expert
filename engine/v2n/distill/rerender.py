"""Re-project an existing ``distill.json``.

Separate from :mod:`v2n.distill.partial` on purpose: that module re-runs a
backend on a time window, this one runs no model at all. Both the CLI and the
MCP server need exactly this behaviour, so it lives here rather than being
copied into each entry point.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from .ir import load_ir, write_ir
from .renderers import render_all


def rerender(lesson_dir: Path | str, cfg: Config,
             outputs: list[str] | None = None) -> list[tuple[str, Path]]:
    """Render the IR already on disk; no audio, no ASR, no model.

    Hotkeys are re-applied first on purpose: a user who just edited the phrase
    list expects the next render to reflect it, without paying for a full run.
    """
    lesson = Path(lesson_dir)
    ir_path = lesson / "distill.json"
    if not ir_path.exists():
        raise FileNotFoundError(
            f"{ir_path} not found; run the lesson with distillation enabled first")
    ir = load_ir(ir_path)
    if getattr(cfg, "hotkeys_enabled", False):
        # Imported here: hotkeys depends on distill, so a module-level import
        # would close the cycle.
        from ..hotkeys import PROJECT_FILE, load_hotkeys
        from ..hotkeys import apply as apply_hotkeys

        keys = load_hotkeys(project=lesson / PROJECT_FILE,
                           user=cfg.hotkeys_user_file)
        if len(keys):
            ir = apply_hotkeys(ir, keys)
            write_ir(ir, ir_path)
    return render_all(ir, lesson, cfg, outputs)
