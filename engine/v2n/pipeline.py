"""Stage orchestration with resume, integrity gate and structured summary.

Stages: ``ingest -> asr -> clean -> frames -> render -> gate``. Each stage emits
an append-only event. A finished lesson (``notes.md`` + gate PASS) is skipped on
re-run unless ``force=True``.
"""
from __future__ import annotations

import json
import math
import os
import wave
from array import array
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .asr import ASRBackend, Segment, load_transcript, save_transcript
from .atomic import atomic_write_text
from .config import Config
from .distill import DistillIR, discard, distil, render_all, write_ir
from .frames import collect_frames
from .hotkeys import PROJECT_FILE as HOTKEYS_FILE
from .hotkeys import apply as apply_hotkeys
from .hotkeys import load_hotkeys
from .ingest import MediaResult, ensure_audio
from .integrity import Verdict, check_wav, playlist_duration
from .render import render_notes, render_transcript_clean, write_run_summary
from .state import Store
from .transcript import CleanResult, TermTable, clean

# Hard floor for distilled coverage. Below this the IR no longer represents the
# lesson, so a "complete-looking" notes file would be misleading.
DISTILL_MIN_COVERAGE = 0.6

# A rewriting backend is judged on discipline, not coverage: rejecting more than
# this fraction means it paraphrased beyond what the transcript supports.
DISTILL_MAX_REJECT_RATIO = 0.8
DISTILL_MIN_CLAIM_DENSITY = 0.3


@dataclass
class LessonResult:
    key: str
    out_dir: Path
    gate_passed: bool
    gate_reasons: list[str]
    meta: dict[str, Any]
    skipped: bool = False


def distill_gate(ir: DistillIR) -> tuple[bool, list[str]]:
    """IR-only quality checks, shared by the pipeline gate and the agent bridge.

    Kept in one place on purpose: ``finalize`` assembles an IR outside a normal
    run, and a second copy of these thresholds would drift until the same IR
    passed in one path and failed in the other.
    """
    reasons: list[str] = []
    stats = ir.stats
    backend = str(stats.get("source", "rules"))
    claims = int(stats.get("claims") or 0)
    if not claims:
        reasons.append("distill produced no traceable claims")
    elif backend == "rules":
        # `rules` claims to preserve everything it keeps, so low coverage
        # means content was silently lost — the failure this project exists
        # to prevent.
        if float(stats.get("coverage", 0.0)) < DISTILL_MIN_COVERAGE:
            reasons.append(f"distill coverage {float(stats['coverage']):.2f} "
                           f"< {DISTILL_MIN_COVERAGE:.2f}")
    else:
        # A rewriting backend compresses on purpose; demanding high coverage
        # would punish the feature for working. Judge it on discipline
        # instead: did it ignore its instructions, or return almost nothing?
        rejected = int(stats.get("untraceable") or 0)
        total = claims + rejected
        if total and rejected / total > DISTILL_MAX_REJECT_RATIO:
            reasons.append(
                f"distiller rejected {rejected}/{total} claims as untraceable "
                f"(> {DISTILL_MAX_REJECT_RATIO:.0%}); it is paraphrasing beyond "
                "what the transcript supports")
        content = int(stats.get("content_segments") or 0)
        if content and claims < max(3, content * DISTILL_MIN_CLAIM_DENSITY):
            reasons.append(
                f"distiller returned only {claims} claims for {content} content "
                "segments; the outline would not represent the lesson")
    if stats.get("untraceable"):
        reasons.append(f"NOTE: dropped {stats['untraceable']} untraceable "
                       "claim(s) rather than softening them")
    hard = [r for r in reasons if not r.startswith("NOTE:")]
    return (not hard), reasons


def _gate(verdict: Verdict, raw: list[Segment],
          cleaned: list[Segment],
          ir: DistillIR | None = None) -> tuple[bool, list[str]]:
    """Structural gate. File-level checks happen after rendering.

    The gate judges the IR only. It never requires that every requested output
    format succeeded: making extra formats mandatory would punish users for
    choosing more, which contradicts "the user decides".
    """
    reasons: list[str] = []
    if not verdict.ok:
        reasons.append(f"integrity FAIL: {verdict.reason} "
                       f"(expected {verdict.expected:.1f}s, actual {verdict.actual:.1f}s)")
    if not raw:
        reasons.append("ASR produced no segments")
    if not cleaned:
        reasons.append("cleanup produced no segments")
    if verdict.reason == "no_reference":
        reasons.append("NOTE: no reference duration supplied; integrity skipped")
    if ir is not None:
        reasons.extend(distill_gate(ir)[1])
    hard = [r for r in reasons if not r.startswith("NOTE:")]
    return (not hard), reasons


def run_lesson(cfg: Config, key: str, out_dir: Path, *,
               source: str | Path | None = None,
               expected_duration: float = 0.0,
               backend: ASRBackend | None = None,
               table: TermTable | None = None,
               store: Store | None = None,
               force: bool = False) -> LessonResult:
    """Run (or resume) one lesson. Returns a structured result, never raises on
    a quality failure — a failed gate is data, not an exception."""
    from .asr import make_backend

    out_dir.mkdir(parents=True, exist_ok=True)
    notes = out_dir / "notes.md"
    summary = out_dir / "run_summary.json"
    if not force and notes.exists() and summary.exists():
        try:
            prev = json.loads(summary.read_text(encoding="utf-8"))
            if prev.get("gate_passed"):
                return LessonResult(key, out_dir, True, ["resumed: already passed"],
                                    prev, skipped=True)
        except (ValueError, OSError):
            pass

    def event(stage: str, status: str, detail: Any = None) -> None:
        if store:
            store.log_event(key, stage, status, detail)

    backend = backend or make_backend(cfg)
    table = table if table is not None else TermTable.load(cfg.terms_file)

    # ---- 1. ingest -----------------------------------------------------
    if source is not None:
        media = ensure_audio(cfg, out_dir, source,
                             expected_duration=expected_duration)
        event("ingest", "done", {"actions": media.actions,
                                 "integrity": media.verdict.as_dict()})
    else:
        wav = out_dir / "audio.wav"
        if not wav.exists():
            raise FileNotFoundError(f"{wav} missing and no --source given")
        v = check_wav(wav, expected_duration, tolerance=cfg.duration_tolerance)
        media = MediaResult(media=wav, wav=wav, expected_duration=expected_duration,
                            verdict=v, actions=["reuse_existing_wav"])
        event("ingest", "done", {"actions": media.actions,
                                 "integrity": v.as_dict()})

    # ---- 2. ASR --------------------------------------------------------
    transcript_path = out_dir / "transcript.txt"
    raw: list[Segment] = []
    if not force and transcript_path.exists() and transcript_path.stat().st_size > 0:
        raw = load_transcript(transcript_path)
        if raw:
            event("asr", "resumed", {"segments": len(raw)})
        else:
            # A killed run can leave "[00:00-" on disk. Reusing it silently
            # produces an empty transcript and a FAIL gate that never recovers.
            transcript_path.unlink()
            event("asr", "discarded_corrupt", {"path": transcript_path.name})
    if not raw:
        raw = backend.transcribe(media.wav, out_dir)
        save_transcript(raw, transcript_path)
        event("asr", "done", {"segments": len(raw), "backend": backend.name})

    # ---- 3. cleanup ----------------------------------------------------
    result: CleanResult = clean(raw, table, cfg)
    render_transcript_clean(result.segments, out_dir / "transcript_clean.md")
    event("clean", "done", {"fixes": result.fixes, "dropped": result.dropped,
                            "merged": result.merged})

    # ---- 4. frames (optional) -----------------------------------------
    frames: list[Path] = []
    frames_dropped = 0
    if cfg.frames_enabled and media.media is not None:
        try:
            frames, frames_dropped = collect_frames(media.media, out_dir, cfg)
            event("frames", "done", {"kept": len(frames), "dropped": frames_dropped})
        except (RuntimeError, FileNotFoundError, OSError) as exc:
            event("frames", "skipped", {"reason": f"{type(exc).__name__}: {exc}"})
    else:
        reason = "disabled" if not cfg.frames_enabled else "no media available"
        event("frames", "skipped", {"reason": reason})

    # ---- 5. distill (IR + user-selected projections) -------------------
    ir: DistillIR | None = None
    distill_outputs: list[tuple[str, Path]] = []
    # Filled in below when distillation runs; the gate is then reused instead of
    # recomputed, so the decision that suppressed the files and the verdict in the
    # summary cannot disagree.
    gate_ok, gate_reasons = True, []
    if cfg.distill_enabled:
        ir = distil(result.segments, cfg, lesson_key=key)
        if cfg.hotkeys_enabled:
            # Emphasis triggers never add text; they only decide which existing
            # claims to point at, so the traceability invariant still holds.
            keys = load_hotkeys(project=out_dir / HOTKEYS_FILE,
                                user=cfg.hotkeys_user_file)
            if len(keys):
                ir = apply_hotkeys(ir, keys)
                event("hotkeys", "applied", {"count": len(keys),
                                             "emphasis": ir.stats.get("emphasis_claims")})
        # Record the cleaning inputs: a later partial re-distil replays the
        # cleaning step from transcript.txt, and without the term table it would
        # silently reintroduce the ASR errors the first run had already fixed.
        ir.stats["terms_file"] = str(getattr(table, "source", None)
                                     or cfg.terms_file or "")
        write_ir(ir, out_dir / "distill.json")
        # Judge before writing, not after. A gate that runs once the files exist
        # can only describe the problem, never prevent it: rendering first meant a
        # failed run still left outline.md on disk, and anything that reads the
        # directory instead of the `gate` field would pick up a 6%-coverage
        # outline that looked finished. `distill.json` is written either way, so
        # nothing is lost — `v2n distill render` re-renders it on request.
        gate_ok, gate_reasons = _gate(media.verdict, raw, result.segments, ir)
        if gate_ok:
            distill_outputs = render_all(ir, out_dir, cfg)
        else:
            # Re-runs are the case that matters: last run's files would survive
            # otherwise, and they look better than the run that just failed.
            for stale in discard(out_dir, cfg.distill_outputs):
                event("distill", "discarded", {"path": str(stale)})
        event("distill", "done",
              {"backend": ir.stats.get("source"),
               "requested": cfg.distill_backend,
               "claims": ir.stats.get("claims"),
               "coverage": ir.stats.get("coverage"),
               "tokens_in": ir.stats.get("tokens_in"),
               "tokens_out": ir.stats.get("tokens_out"),
               "rendered": gate_ok,
               "outputs": [name for name, _ in distill_outputs]})
    else:
        event("distill", "skipped", {"reason": "disabled"})

    # ---- 6. render + gate ---------------------------------------------
    meta: dict[str, Any] = {
        "lesson_key": key,
        "backend": backend.name,
        "expected_duration": media.expected_duration,
        "integrity": media.verdict.as_dict(),
        "raw_segments": len(raw),
        "clean_segments": len(result.segments),
        "fixes": result.fixes,
        "dropped": result.dropped,
        "merged": result.merged,
        "frames_kept": len(frames),
        "frames_dropped": frames_dropped,
        "ingest_actions": media.actions,
        "distill": (
            {"enabled": True,
             "requested": cfg.distill_backend,
             # `auto` silently choosing rules is fine in code, never in output:
             # a user who configured nothing must not believe they got a distilled
             # outline when they got a cleaned transcript.
             "degraded": (cfg.distill_backend != "rules"
                          and ir.stats.get("source") == "rules"),
             "outputs": [name for name, _ in distill_outputs],
             # Empty `outputs` plus false `rendered` is the honest pair: the IR
             # exists, the projected files were withheld because the gate failed.
             "rendered": gate_ok,
             "stats": ir.stats}
            if ir is not None else {"enabled": False}
        ),
    }
    ok, reasons = ((gate_ok, gate_reasons) if ir is not None
                   else _gate(media.verdict, raw, result.segments, None))
    meta["gate_passed"] = ok
    meta["gate_reasons"] = reasons
    render_notes(meta, result.segments, frames, notes, cfg)
    if not notes.exists() or notes.stat().st_size < 80:
        ok = False
        reasons = [*reasons, "notes.md missing or too small after rendering"]
        meta["gate_passed"], meta["gate_reasons"] = ok, reasons
        render_notes(meta, result.segments, frames, notes, cfg)
    write_run_summary(meta, summary)
    event("gate", "passed" if ok else "failed", {"reasons": reasons})
    if store:
        store.upsert_trace(key, {"gate_passed": ok,
                                 "segments": len(result.segments),
                                 "integrity": media.verdict.as_dict()})
    return LessonResult(key, out_dir, ok, reasons, meta)


def make_demo_wav(path: Path, *, seconds: float = 60.0,
                  sample_rate: int = 16000, freq: float = 440.0) -> Path:
    """Write a deterministic synthetic PCM WAV atomically (no external assets)."""
    if seconds <= 0:
        raise ValueError(f"seconds must be > 0, got {seconds}")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * sample_rate)
    samples = array("h", (int(12000 * math.sin(2 * math.pi * freq * i / sample_rate))
                          for i in range(n)))
    if samples.itemsize != 2:
        raise RuntimeError("this platform's array('h') is not 16-bit")
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(samples.tobytes())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def make_demo_playlist(path: Path, *, seconds: float = 60.0,
                       segment_seconds: float = 10.0) -> Path:
    """A synthetic media playlist whose EXTINF sum equals the WAV duration."""
    if seconds <= 0 or segment_seconds <= 0:
        raise ValueError("seconds and segment_seconds must be > 0")
    count = max(1, int(seconds // segment_seconds))
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for _ in range(count):
        lines += [f"#EXTINF:{segment_seconds:.1f},", "segment.ts"]
    lines.append("#EXT-X-ENDLIST")
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def run_demo(cfg: Config, out_dir: Path, *, seconds: float = 60.0) -> LessonResult:
    """Fully offline end-to-end run: synthetic WAV -> fake ASR -> cleaned notes.

    No ffmpeg, no model, no network, no third-party media. This is the command
    CI runs on every push. The reference duration is read back from the
    generated playlist's EXTINF sum, so the demo exercises the real integrity
    path rather than hard-coding the number it is supposed to verify.
    """
    from .asr import FakeBackend

    out_dir.mkdir(parents=True, exist_ok=True)
    make_demo_wav(out_dir / "audio.wav", seconds=seconds)
    pl_path = make_demo_playlist(out_dir / "demo.m3u8", seconds=seconds)
    expected = playlist_duration(pl_path.read_text(encoding="utf-8"))
    if expected <= 0:
        raise RuntimeError("generated demo playlist has no EXTINF duration")
    # Never mutate the caller's Config: it may be reused for a real lesson.
    demo_cfg = replace(cfg, work_dir=out_dir, asr_backend="fake",
                       frames_enabled=False)
    table = TermTable.load(Path(__file__).parent / "terms.example.yaml")
    return run_lesson(demo_cfg, "demo-lesson", out_dir, source=None,
                      expected_duration=expected, backend=FakeBackend(),
                      table=table, force=True)
