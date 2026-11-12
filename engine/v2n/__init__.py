"""video2notes — offline-first, resumable video-to-notes pipeline.

Public API is intentionally small: :class:`~v2n.config.Config`,
:func:`~v2n.pipeline.run_lesson`, :func:`~v2n.pipeline.run_demo` and the
:mod:`v2n.cli` entry point. Everything else is an implementation detail.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .config import Config
from .state import Store

__all__ = ["Config", "Store", "__version__"]
