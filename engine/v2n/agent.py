"""Agent bridge: let an MCP host do the distilling.

**Why this exists.** ``rules`` and ``llm`` both assume the engine can *reach* a
model: one is local and deterministic, the other speaks ``/chat/completions``.
An agent host breaks that assumption. A tool call is a synchronous
request/response, so the engine cannot ask the host a question and wait for the
answer inside a function — the host is the caller, not a service the engine can
dial. Control therefore has to be inverted:

============  ===============================================================
``prepare``   cleaned segments -> self-contained work orders on disk
``submit``    the host's answer -> per-claim validation against the transcript
``finalize``  accepted claims -> the same IR, gate and renderers as any backend
============  ===============================================================

What does **not** change is the validator. A claim must still be a substring of,
or overlap by at least ``OVERLAP_MIN``, the segment it cites; otherwise it is
dropped and counted in ``stats.untraceable``. The host may be creative; the
checker is not.

Three properties are deliberate rather than incidental:

* **Timestamp-free artefacts.** Re-running ``prepare`` on the same transcript
  produces byte-identical work orders, so a diff means the input changed — not
  that the clock moved.
* **Idempotent submission.** A chunk's result *replaces* the previous one
  instead of appending, so a retried tool call cannot double the claims.
* **Spans are not trusted.** Only the segment index is taken from the host;
  ``start``/``end`` always come from the transcript. A host that invents times
  cannot shift a claim onto a different part of the lesson.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .asr import Segment, load_transcript
from .atomic import atomic_write_text
from .config import Config
from .distill import discard, render_all, write_ir
from .distill.ir import (
    DEDUPE_SIMILARITY,
    content_segments,
    group_topics,
    is_traceable,
    similarity,
)
from .distill.model import KINDS, Claim, DistillIR
from .distill.rules import detect_language, score
from .transcript import TermTable, clean

AGENT_DIR = "_agent"
MANIFEST = "manifest.json"
SCHEMA_VERSION = 1
DEFAULT_CHUNK_SIZE = 20
# Fabricated claims are quoted back to the caller so the host can see what it
# got wrong; cap the echo so one runaway answer cannot bloat the result file.
ECHO_MAX = 160


def work_dir(lesson_dir: Path | str) -> Path:
    return Path(lesson_dir) / AGENT_DIR


def chunk_path(lesson_dir: Path | str, chunk_id: str) -> Path:
    return work_dir(lesson_dir) / f"{chunk_id}.json"


def result_path(lesson_dir: Path | str, chunk_id: str) -> Path:
    return work_dir(lesson_dir) / f"{chunk_id}.result.json"


def _chunk_id(index: int) -> str:
    return f"chunk-{index:03d}"


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> Path:
    """Sorted keys, no clock: the same input must serialise to the same bytes."""
    return atomic_write_text(
        path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def fingerprint(segments: list[Segment]) -> str:
    """Stable digest of the cleaned transcript a work order was built from.

    Used to detect the one dangerous case: work orders survive, but the
    transcript underneath them was re-transcribed or re-cleaned. Submitting
    answers to stale orders would silently attach claims to the wrong segments.
    """
    digest = hashlib.sha256()
    for segment in segments:
        digest.update(f"{segment.start:.3f}\t{segment.end:.3f}\t{segment.text}\n"
                      .encode())
    return digest.hexdigest()


def load_manifest(lesson_dir: Path | str) -> dict[str, Any]:
    path = work_dir(lesson_dir) / MANIFEST
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found; run `v2n agent prepare --lesson {lesson_dir}` first")
    data = _read_json(path)
    if int(data.get("schema", 0)) != SCHEMA_VERSION:
        raise ValueError(f"unsupported agent manifest schema in {path}")
    return data


def load_segments(lesson_dir: Path | str, cfg: Config) -> list[Segment]:
    """Cleaned segments of a lesson, replayed from ``transcript.txt``.

    The term table is taken from an existing ``distill.json`` when the caller
    did not pass one: replaying ``clean`` without it would resurrect the ASR
    errors the first run had already corrected.
    """
    lesson = Path(lesson_dir)
    path = lesson / "transcript.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found; run the lesson once so a transcript exists")
    terms = cfg.terms_file
    if terms is None:
        ir_path = lesson / "distill.json"
        if ir_path.exists():
            stored = ""
            try:
                from .distill import load_ir

                stored = str(load_ir(ir_path).stats.get("terms_file") or "")
            except (ValueError, OSError, KeyError):
                stored = ""
            if stored:
                terms = Path(stored)
    return clean(load_transcript(path), TermTable.load(terms), cfg).segments


def _lesson_key(lesson: Path) -> str:
    """Prefer the key the lesson was actually run with.

    Falling back to the directory name would give the IR a different
    ``lesson_key`` than ``run_summary.json``, and a later partial re-distil
    would then write claims under a key nothing else references.
    """
    summary = lesson / "run_summary.json"
    if summary.exists():
        try:
            stored = str(_read_json(summary).get("lesson_key") or "")
        except (ValueError, OSError):
            stored = ""
        if stored:
            return stored
    return lesson.name


def prepare(lesson_dir: Path | str, segments: list[Segment], *,
            chunk_size: int | None = None, force: bool = False,
            lesson_key: str = "") -> dict[str, Any]:
    """Write one self-contained work order per chunk. Idempotent."""
    from .distill.backends import build_prompt, system_prompt

    lesson = Path(lesson_dir)
    if not segments:
        raise ValueError("no cleaned segments: nothing to distil")
    size = int(chunk_size or DEFAULT_CHUNK_SIZE)
    if size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {size}")

    base = work_dir(lesson)
    base.mkdir(parents=True, exist_ok=True)
    digest = fingerprint(segments)
    manifest_path = base / MANIFEST

    if manifest_path.exists() and not force:
        existing = _read_json(manifest_path)
        same = (existing.get("fingerprint") == digest
                and int(existing.get("chunk_size") or 0) == size)
        if same:
            return _summary(lesson, existing)
        raise ValueError(
            "work orders no longer match this transcript "
            f"({existing.get('segments')} segments then, {len(segments)} now; "
            f"chunk_size {existing.get('chunk_size')} -> {size}). "
            "Re-run with force to rebuild them.")

    if force:
        for stale in sorted(base.glob("chunk-*")):
            stale.unlink(missing_ok=True)

    key = lesson_key or _lesson_key(lesson)
    pairs = list(enumerate(segments))
    chunks: list[dict[str, Any]] = []
    for start in range(0, len(pairs), size):
        block = pairs[start:start + size]
        cid = _chunk_id(len(chunks) + 1)
        order = {
            "schema": SCHEMA_VERSION,
            "chunk": cid,
            "lesson_key": key,
            "instruction": {
                "system": system_prompt(),
                "user": build_prompt(block),
            },
            "segments": [
                {"seg": index, "start": round(float(segment.start), 3),
                 "end": round(float(segment.end), 3), "text": segment.text}
                for index, segment in block
            ],
            "answer_format": {
                "type": "json_array",
                "item": {"seg": "int", "text": "string", "kind": "|".join(KINDS)},
                "max_items": len(block),
                "cite_rule": "one item cites exactly one segment; do not merge "
                             "segments, and keep the speaker's wording",
            },
        }
        _write_json(chunk_path(lesson, cid), order)
        chunks.append({"id": cid, "start_idx": block[0][0],
                       "end_idx": block[-1][0], "count": len(block)})

    manifest = {
        "schema": SCHEMA_VERSION,
        "lesson_key": key,
        "fingerprint": digest,
        "chunk_size": size,
        "segments": len(segments),
        "chunks": chunks,
    }
    _write_json(manifest_path, manifest)
    return _summary(lesson, manifest)


def _normalise_payload(payload: Any) -> list[Any]:
    """Accept a JSON array, a ``{"claims": [...]}`` wrapper, or raw JSON text."""
    from .distill.backends import _extract_json

    if isinstance(payload, str):
        payload = _extract_json(payload)
    if isinstance(payload, dict):
        for key in ("claims", "items", "results", "data"):
            if isinstance(payload.get(key), list):
                return list(payload[key])
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError("payload must be a JSON array of {seg, text, kind} items")


def submit(lesson_dir: Path | str, chunk_id: str, payload: Any,
           segments: list[Segment]) -> dict[str, Any]:
    """Validate one chunk's answers and persist them, replacing any earlier run."""
    lesson = Path(lesson_dir)
    manifest = load_manifest(lesson)
    entry = next((c for c in manifest.get("chunks") or [] if c["id"] == chunk_id), None)
    if entry is None:
        available = ", ".join(c["id"] for c in manifest.get("chunks") or []) or "none"
        raise ValueError(f"unknown chunk {chunk_id!r}; available: {available}")
    if manifest.get("fingerprint") != fingerprint(segments):
        raise ValueError(
            "this work order was built from a different transcript; "
            "re-run `v2n agent prepare --force` before submitting")

    low, high = int(entry["start_idx"]), int(entry["end_idx"])
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    kept: list[str] = []

    for raw in _normalise_payload(payload):
        if not isinstance(raw, dict):
            rejected.append({"reason": "malformed", "detail": str(raw)[:ECHO_MAX]})
            continue
        raw_seg = next((raw[k] for k in ("seg", "segment", "index", "id") if k in raw), None)
        raw_text = next((raw[k] for k in ("text", "content", "claim") if k in raw), "")
        try:
            index = int(raw_seg)
        except (TypeError, ValueError):
            rejected.append({"reason": "malformed", "detail": "missing or non-integer seg"})
            continue
        text = str(raw_text or "").strip()
        if not text:
            rejected.append({"reason": "malformed", "seg": index, "detail": "empty text"})
            continue
        if not low <= index <= high:
            rejected.append({"reason": "out_of_range", "seg": index,
                             "text": text[:ECHO_MAX]})
            continue
        if not is_traceable(text, segments[index].text):
            rejected.append({"reason": "not_traceable", "seg": index,
                             "text": text[:ECHO_MAX]})
            continue
        if any(similarity(text, prior) >= DEDUPE_SIMILARITY for prior in kept):
            rejected.append({"reason": "duplicate", "seg": index,
                             "text": text[:ECHO_MAX]})
            continue
        kept.append(text)
        kind = str(raw.get("kind") or "fact")
        accepted.append({
            "seg": index,
            "text": text,
            "kind": kind if kind in KINDS else "fact",
            "start": round(float(segments[index].start), 3),
            "end": round(float(segments[index].end), 3),
        })

    untraceable = sum(1 for r in rejected if r["reason"] != "duplicate")
    duplicates = sum(1 for r in rejected if r["reason"] == "duplicate")
    result = {
        "schema": SCHEMA_VERSION,
        "chunk": chunk_id,
        "accepted": accepted,
        "rejected": rejected,
        "counts": {"accepted": len(accepted), "rejected": len(rejected),
                   "untraceable": untraceable, "duplicates": duplicates},
    }
    _write_json(result_path(lesson, chunk_id), result)
    summary = _summary(lesson, manifest)
    summary.update({"chunk": chunk_id, "accepted": len(accepted),
                    "rejected": rejected, "counts": result["counts"]})
    return summary


def _summary(lesson: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Progress over all chunks, derived from the result files themselves.

    There is no separate index to fall out of sync: a chunk is submitted iff its
    result file exists.
    """
    chunks = manifest.get("chunks") or []
    submitted = 0
    pending: list[str] = []
    claims = untraceable = duplicates = 0
    for entry in chunks:
        path = result_path(lesson, entry["id"])
        if not path.exists():
            pending.append(entry["id"])
            continue
        submitted += 1
        counts = (_read_json(path) or {}).get("counts") or {}
        claims += int(counts.get("accepted") or 0)
        untraceable += int(counts.get("untraceable") or 0)
        duplicates += int(counts.get("duplicates") or 0)
    return {
        "lesson_key": manifest.get("lesson_key", ""),
        "segments": int(manifest.get("segments") or 0),
        "chunk_size": manifest.get("chunk_size"),
        "chunks": len(chunks),
        "submitted": submitted,
        "pending": pending,
        "claims": claims,
        "untraceable": untraceable,
        "duplicates": duplicates,
        "ready": bool(chunks) and not pending and claims > 0,
    }


def status(lesson_dir: Path | str) -> dict[str, Any]:
    lesson = Path(lesson_dir)
    return _summary(lesson, load_manifest(lesson))


def build_ir(lesson_dir: Path | str, segments: list[Segment], cfg: Config, *,
             model: str = "") -> DistillIR:
    """Assemble the IR from accepted answers, re-validating every claim.

    Re-validation is not redundant: a result file may have been written against
    a transcript that was later re-cleaned, and trusting it would let a claim
    drift onto a segment it never came from.
    """
    lesson = Path(lesson_dir)
    manifest = load_manifest(lesson)
    lang = detect_language(" ".join(s.text for s in segments))
    claims: list[Claim] = []
    kept: list[str] = []
    covered: set[int] = set()
    pending: list[str] = []
    untraceable = duplicates = 0

    for entry in manifest.get("chunks") or []:
        path = result_path(lesson, entry["id"])
        if not path.exists():
            pending.append(entry["id"])
            continue
        data = _read_json(path) or {}
        for row in data.get("rejected") or []:
            if row.get("reason") != "duplicate":
                untraceable += 1
        for item in data.get("accepted") or []:
            try:
                index = int(item["seg"])
                text = str(item.get("text") or "").strip()
            except (KeyError, TypeError, ValueError):
                untraceable += 1
                continue
            if not text or not 0 <= index < len(segments):
                untraceable += 1
                continue
            if not is_traceable(text, segments[index].text):
                untraceable += 1
                continue
            if any(similarity(text, prior) >= DEDUPE_SIMILARITY for prior in kept):
                duplicates += 1
                continue
            kept.append(text)
            kind = str(item.get("kind") or "fact")
            kind = kind if kind in KINDS else "fact"
            claims.append(Claim(
                id=f"c{len(claims) + 1}", text=text, kind=kind, span=(index, index),
                score=score(text, kind),
                start=float(segments[index].start), end=float(segments[index].end)))
            covered.add(index)

    if not claims:
        raise RuntimeError(
            "no accepted claim yet; submit the remaining chunks first "
            f"({len(pending)} pending)" if pending else
            "no accepted claim survived validation; check `v2n agent status`")

    topics = group_topics(claims, max_per_topic=cfg.distill_max_claims_per_topic,
                          lang=lang)
    content = content_segments(segments, cfg, lang)
    total_chars = sum(len(s.text) for s in segments)
    kept_chars = sum(len(c.text) for c in claims)
    stats: dict[str, Any] = {
        "source": "agent",
        "language": lang,
        "segments": len(segments),
        "content_segments": len(content),
        "claims": len(claims),
        "topics": len(topics),
        "skipped_short": 0,
        "untraceable": untraceable,
        "duplicates": duplicates,
        "coverage": (round(len(covered & content) / len(content), 4)
                     if content else 0.0),
        "compression": round(kept_chars / total_chars, 4) if total_chars else 0.0,
        # No token counts: the host pays for them and does not report back.
        # Writing 0 would claim "free", and this project treats "unknown" and
        # "free" as different answers.
        "tokens_in": None,
        "tokens_out": None,
        "cost": None,
        "provider": "agent",
    }
    if model:
        stats["model"] = model
    if pending:
        stats["pending_chunks"] = pending
    return DistillIR(lesson_key=str(manifest.get("lesson_key") or ""),
                     topics=topics, stats=stats)


def finalize(lesson_dir: Path | str, cfg: Config, *, outputs: list[str] | None = None,
             segments: list[Segment] | None = None,
             model: str = "") -> dict[str, Any]:
    """Build the IR from submitted chunks, write ``distill.json`` and render."""
    lesson = Path(lesson_dir)
    if segments is None:
        segments = load_segments(lesson, cfg)
    ir = build_ir(lesson, segments, cfg, model=model)
    if getattr(cfg, "hotkeys_enabled", False):
        # Imported here: hotkeys depends on distill, so a module-level import
        # would close the cycle.
        from .hotkeys import PROJECT_FILE, load_hotkeys
        from .hotkeys import apply as apply_hotkeys

        keys = load_hotkeys(project=lesson / PROJECT_FILE,
                           user=cfg.hotkeys_user_file)
        if len(keys):
            ir = apply_hotkeys(ir, keys)
    # Same field the pipeline records, so a later partial re-distil replays the
    # cleaning step with the term table this IR was built with.
    ir.stats["terms_file"] = str(getattr(cfg, "terms_file", "") or "")
    write_ir(ir, lesson / "distill.json")

    # Imported here to keep this module importable before the pipeline exists.
    from .pipeline import distill_gate

    # Gate before render, for the same reason as a normal run: rendering first
    # and judging afterwards can only label a bad outline, not withhold it. A
    # host that reads files instead of `gate` would then hand the user a
    # 6%-coverage outline with a passing-looking filename.
    passed, reasons = distill_gate(ir)
    if passed:
        produced = render_all(ir, lesson, cfg, outputs)
    else:
        produced = []
        # Mirror what render_all would have written, not what the caller asked
        # for: a partly-requested format must not leave the others behind.
        names = outputs if outputs is not None else cfg.distill_outputs
        discarded = discard(lesson, names)
        # Say where the material went. Silence here reads as "nothing to see",
        # while the IR on disk is exactly what a human needs to diagnose it.
        detail = (f"withheld and deleted {len(discarded)} previously rendered "
                  "file(s)" if discarded else "withheld every rendered file")
        reasons = [*reasons,
                   (f"{detail}; distill.json is intact; "
                    "`v2n distill render --lesson <dir> --force` re-renders it "
                    "deliberately")]
    return {
        "claims": ir.stats["claims"],
        "topics": ir.stats["topics"],
        "coverage": ir.stats["coverage"],
        "compression": ir.stats["compression"],
        "untraceable": ir.stats["untraceable"],
        "duplicates": ir.stats["duplicates"],
        "outputs": [name for name, _ in produced],
        "rendered": passed,
        "gate": "PASS" if passed else "FAIL",
        "reasons": reasons,
        "pending": ir.stats.get("pending_chunks") or [],
    }
