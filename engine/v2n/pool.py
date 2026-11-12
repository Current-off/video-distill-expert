"""Bounded worker pool with per-item logs and a machine-readable summary.

Measured on a 16-logical-core / 8-physical-core Windows host running CTranslate2
ASR: 5 workers x 3 threads each consumed 12.8 cores but delivered only ~1.6x
throughput versus one serial worker, because intra-op threads contend with each
other. Fewer workers with more threads each (2-3 x 6-8) is the better starting
point. :func:`recommended_workers` encodes that heuristic; tune with real data.

Log routing
-----------
``contextlib.redirect_stdout`` is process-global. With several worker threads,
one thread restores ``sys.stdout`` to a file object that another thread has
already closed, producing ``ValueError: I/O operation on closed file`` and even
``lost sys.stderr``. Instead we install one proxy per stream and keep the
per-task destination in a :class:`~contextvars.ContextVar`, so each task writes
only to its own log and the streams are restored exactly once.
"""
from __future__ import annotations

import contextvars
import json
import os
import sys
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TextIO

from .atomic import atomic_write_text

_LOG_TARGET: contextvars.ContextVar[TextIO | None] = contextvars.ContextVar(
    "v2n_log_target", default=None)


class _RoutingStream:
    """Forward writes to the current task's log, or to the original stream."""

    def __init__(self, original: TextIO):
        object.__setattr__(self, "_original", original)

    def _target(self) -> TextIO:
        orig: TextIO = object.__getattribute__(self, "_original")
        return _LOG_TARGET.get() or orig

    def write(self, s: str) -> int:
        try:
            return self._target().write(s)
        except (ValueError, OSError):
            # A task log was closed underneath us; never lose the message and
            # never crash the process because of logging.
            orig: TextIO = object.__getattribute__(self, "_original")
            return orig.write(s)

    def flush(self) -> None:
        try:
            self._target().flush()
        except (ValueError, OSError):
            orig: TextIO = object.__getattribute__(self, "_original")
            orig.flush()

    def __getattr__(self, name: str) -> Any:
        orig: TextIO = object.__getattribute__(self, "_original")
        return getattr(orig, name)


def recommended_workers(logical_cores: int | None = None,
                        threads_per_worker: int = 6,
                        *, max_workers: int = 4) -> int:
    """Conservative default: never oversubscribe logical cores."""
    cores = logical_cores or os.cpu_count() or 4
    return max(1, min(max_workers, cores // max(1, threads_per_worker)))


def run_pool(items: list[Any],
             worker: Callable[[Any, Path], dict[str, Any]],
             *, workers: int, log_dir: Path) -> dict[str, Any]:
    """Execute ``worker(item, log_path)`` for every item; never raises on item
    failure — each failure is recorded with its traceback and returned."""
    log_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    t0 = time.time()

    def one(item: Any) -> dict[str, Any]:
        name = str(item.get("key") if isinstance(item, dict) else item)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        log = log_dir / f"{safe}.log"
        start = time.time()
        with log.open("w", encoding="utf-8", errors="replace") as fh:
            token = _LOG_TARGET.set(fh)
            try:
                result = worker(item, log)
                rec = {"key": name, "exit": 0,
                       "seconds": round(time.time() - start, 2), **result}
            except Exception as exc:  # one bad item must not abort the batch
                fh.write(traceback.format_exc())
                rec = {"key": name, "exit": 1,
                       "seconds": round(time.time() - start, 2),
                       "error": f"{type(exc).__name__}: {exc}"}
            finally:
                _LOG_TARGET.reset(token)
        return rec

    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout = _RoutingStream(original_out)  # type: ignore[assignment]
    sys.stderr = _RoutingStream(original_err)  # type: ignore[assignment]
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = [ex.submit(one, it) for it in items]
            for fut in as_completed(futures):
                rows.append(fut.result())
    finally:
        sys.stdout, sys.stderr = original_out, original_err

    rows.sort(key=lambda r: str(r.get("key")))
    summary = {
        "total": len(rows),
        "ok": sum(1 for r in rows if r.get("exit") == 0),
        "seconds": round(time.time() - t0, 2),
        "workers": workers,
        "rows": rows,
    }
    atomic_write_text(log_dir / "summary.json",
                      json.dumps(summary, ensure_ascii=False, indent=2))
    return summary
