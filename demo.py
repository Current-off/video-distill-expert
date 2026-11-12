#!/usr/bin/env python3
"""End-to-end self-check: transcript in, three note formats out.

This is the deployment's own test, deliberately not a mock. It exercises the
same four steps a host would (prepare -> get_chunk -> submit -> finalize), with
answers produced by a *dumb* strategy: quote the first sentence of each segment
verbatim. Nothing is rewritten, so every claim passes the traceability gate for
the right reason rather than by luck.

What that proves: the engine, the work-order bridge, the validator and the
renderers are all wired correctly. What it does not prove: that a model produces
good notes — that depends on you, the host, and no self-test can stand in for it.

Needs no network, no ffmpeg, no ASR model, no API key.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
SAMPLE = REPO / "examples" / "sample_transcript.txt"
DEFAULT_ROOT = Path.home() / ".video-distill-expert"


def ensure_engine(root: Path) -> None:
    """Re-exec under the deployment's interpreter if this one lacks v2n.

    A user who ran `python demo.py` with their system Python would otherwise get
    an ImportError that looks like a broken deployment. The package is installed
    in a venv on purpose, so look there rather than telling them to install twice.
    """
    try:
        import v2n  # noqa: F401
        return
    except ImportError:
        pass
    for venv in (root / "venv", DEFAULT_ROOT / "venv"):
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if py.exists():
            sys.stderr.write(f"note  using the deployment interpreter {py}\n")
            raise SystemExit(subprocess.run(
                [str(py), str(Path(__file__).resolve()), *sys.argv[1:]],
                check=False).returncode)
    sys.stderr.write(
        "FAIL v2n is not importable and no deployment venv was found.\n"
        "     run: python bootstrap.py    (then re-run this script)\n")
    raise SystemExit(2)


def first_sentence(text: str) -> str:
    """Longest leading run ending at a sentence break; always a verbatim prefix.

    Slicing rather than rewriting is the point: a paraphrase would be rejected
    by the validator, and the demo must fail loudly if the validator is wrong,
    not silently succeed because the sample was hand-tuned.
    """
    best = ""
    for sep in ("。", "！", "？", "；"):
        idx = text.find(sep)
        if idx + 1 > len(best):
            best = text[: idx + 1]
    if len(best) < 12:
        for sep in (". ", "; "):
            idx = text.find(sep)
            if idx + 2 > len(best):
                best = text[: idx + 2]
    return (best or text).strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the distillation pipeline end to end.")
    ap.add_argument("--work", default=None,
                    help="lesson directory (default: a fresh temp dir)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT),
                    help="deployment root holding the venv")
    ap.add_argument("--chunk-size", type=int, default=6,
                    help="segments per work order (default: 6, small to exercise batching)")
    ap.add_argument("--keep", action="store_true", help="keep the lesson directory")
    args = ap.parse_args(argv)

    ensure_engine(Path(args.root).expanduser())

    from v2n.agent import (
        chunk_path,
        finalize,
        load_manifest,
        load_segments,
        prepare,
        status,
        submit,
    )
    from v2n.config import Config

    work = Path(args.work).expanduser().resolve() if args.work else Path(
        tempfile.mkdtemp(prefix="vde-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    if not SAMPLE.exists():
        print(f"FAIL sample transcript missing: {SAMPLE}")
        return 2
    (work / "transcript.txt").write_text(SAMPLE.read_text(encoding="utf-8"),
                                         encoding="utf-8")
    print(f"work  {work}")

    cfg = Config.load(None, work_dir=work)
    segments = load_segments(work, cfg)
    print(f"read  {len(segments)} cleaned segments from transcript.txt")

    info = prepare(work, segments, chunk_size=args.chunk_size)
    print(f"plan  {info['chunks']} work order(s), {info['pending']} pending")

    chunk_ids = [c["id"] for c in load_manifest(work).get("chunks") or []]
    total = 0
    for chunk_id in chunk_ids:
        order = json.loads(chunk_path(work, chunk_id).read_text(encoding="utf-8"))
        claims = [{"seg": s["seg"], "text": first_sentence(s["text"]), "kind": "fact"}
                  for s in order["segments"]]
        result = submit(work, chunk_id, claims, segments)
        kept = int(result.get("accepted") or 0)
        total += kept
        print(f"  {chunk_id}: submitted {len(claims)}, accepted {kept}")

    state = status(work)
    if state.get("pending"):
        print(f"FAIL chunks still pending: {state['pending']}")
        return 4

    final = finalize(work, cfg, outputs=["outline", "timeline", "cheatsheet"])
    gate = str(final.get("gate") or "").upper()
    print(f"\nclaims {total}  coverage {final.get('coverage')}  "
          f"untraceable {final.get('untraceable')}  gate {gate}")
    for name in final.get("outputs") or []:
        print(f"  {name:11} {work / f'{name}.md'}")

    if gate != "PASS":
        print("\nreasons:")
        for reason in final.get("reasons") or ["(none reported)"]:
            print(f"  - {reason}")
        return 4

    outline = work / "outline.md"
    print("\n--- outline.md (head) ---")
    for line in outline.read_text(encoding="utf-8").splitlines()[:12]:
        print(line)

    if args.keep:
        print(f"\nkept  {work}")
    else:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        print("\n(lesson directory removed; pass --keep to inspect it)")
    print("OK    deployment verified end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
