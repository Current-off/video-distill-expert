"""Command line interface. Run ``v2n --help`` or ``python -m v2n --help``."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml

from . import __version__
from .config import Config


def _cfg(args: argparse.Namespace, **overrides) -> Config:
    return Config.load(getattr(args, "config", None), **overrides)


def _apply_distill_args(cfg: Config, args: argparse.Namespace) -> None:
    """CLI overrides for the distill stage; unknown names fail later, loudly."""
    if getattr(args, "no_distill", False):
        cfg.distill_enabled = False
    raw = getattr(args, "outputs", None)
    if raw:
        cfg.distill_outputs = [x.strip() for x in str(raw).split(",") if x.strip()]
    backend = getattr(args, "distiller", None)
    if backend:
        cfg.distill_backend = backend


def cmd_demo(args: argparse.Namespace) -> int:
    from .pipeline import run_demo

    out = Path(args.out).resolve()
    if args.seconds <= 0:
        raise ValueError(f"--seconds must be > 0, got {args.seconds}")
    cfg = _cfg(args, work_dir=out, state_db=out / "state.db")
    _apply_distill_args(cfg, args)
    r = run_demo(cfg, out, seconds=args.seconds)
    print(f"lesson      : {r.key}")
    print(f"gate        : {'PASS' if r.gate_passed else 'FAIL'}")
    print(f"integrity   : {r.meta['integrity']['reason']} "
          f"(expected {r.meta['expected_duration']:.1f}s, "
          f"actual {r.meta['integrity']['actual']:.1f}s, "
          f"deviation {r.meta['integrity']['deviation']:+.4f})")
    print(f"segments    : {r.meta['raw_segments']} raw -> "
          f"{r.meta['clean_segments']} clean")
    print(f"term fixes  : {r.meta['fixes']}  dropped={r.meta['dropped']}  "
          f"merged={r.meta['merged']}")
    dist = r.meta.get("distill") or {}
    if dist.get("enabled"):
        stats = dist.get("stats") or {}
        print(f"distill     : {stats.get('claims', 0)} claims / "
              f"{stats.get('topics', 0)} topics, coverage "
              f"{float(stats.get('coverage', 0)) * 100:.0f}%, "
              f"compression {float(stats.get('compression', 0)):.2f} "
              f"({stats.get('source', 'rules')})")
        print(f"              formats: {', '.join(dist.get('outputs') or [])}")
    print(f"outputs     : {out / 'transcript.txt'}")
    print(f"              {out / 'transcript_clean.md'}")
    print(f"              {out / 'notes.md'}")
    print(f"              {out / 'run_summary.json'}")
    for reason in r.gate_reasons:
        print(f"  note: {reason}")
    _report_usage(r.meta)
    return 0 if r.gate_passed else 1


def cmd_run(args: argparse.Namespace) -> int:
    from .asr import make_backend
    from .pipeline import run_lesson
    from .state import Store
    from .transcript import TermTable

    out = Path(args.out).resolve()
    if args.expected_duration < 0:
        raise ValueError("--expected-duration must be >= 0")
    if args.threads is not None and args.threads < 1:
        raise ValueError("--threads must be >= 1")
    out.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(args, work_dir=out, state_db=out / "state.db")
    _apply_distill_args(cfg, args)
    if args.asr:
        cfg.asr_backend = args.asr
    if getattr(args, "lang", None):
        cfg.asr_language = args.lang
    if args.frames:
        cfg.frames_enabled = True
    if args.threads:
        cfg.asr_threads = args.threads
    if args.terms:
        cfg.terms_file = Path(args.terms)
    key = args.key or out.name
    with Store(cfg.state_db) as store:
        r = run_lesson(
            cfg, key, out, source=args.source,
            expected_duration=args.expected_duration,
            backend=make_backend(cfg), table=TermTable.load(cfg.terms_file),
            store=store, force=args.force,
        )
    print(f"lesson  : {r.key}")
    print(f"gate    : {'PASS' if r.gate_passed else 'FAIL'}")
    print(f"notes   : {out / 'notes.md'}")
    dist = r.meta.get("distill") or {}
    if dist.get("enabled"):
        print(f"distill : {out / 'distill.json'} "
              f"({', '.join(dist.get('outputs') or [])})")
    print(f"summary : {out / 'run_summary.json'}")
    for reason in r.gate_reasons:
        print(f"  - {reason}")
    if not cfg.terms_file:
        print("note    : no terms file supplied; term correction is disabled "
              "(see config.example.yaml)")
    _report_usage(r.meta)
    return 0 if r.gate_passed else 1


def cmd_batch(args: argparse.Namespace) -> int:
    from .pipeline import run_lesson
    from .pool import run_pool
    from .state import Store

    manifest = Path(args.manifest)
    items = yaml.safe_load(manifest.read_text(encoding="utf-8")) or []
    if isinstance(items, dict):
        items = items.get("lessons", [])
    if not isinstance(items, list) or not items:
        print("manifest must be a non-empty YAML list of {key, source}", file=sys.stderr)
        return 2
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be >= 1")
    root = Path(args.out_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    log_dir = Path(args.logs).resolve() if args.logs else root / "_logs"
    base = _cfg(args)

    # run_pool fixes this signature; `log` is intentionally unused here because
    # each lesson owns its own run_summary.json instead of a shared log.
    def worker(item: dict, log: Path) -> dict:  # noqa: ARG001
        key = str(item["key"])
        out = root / key
        cfg = Config.load(args.config, work_dir=out, state_db=out / "state.db")
        if item.get("asr"):
            cfg.asr_backend = item["asr"]
        if item.get("terms"):
            cfg.terms_file = Path(item["terms"])
        # One connection per lesson: sqlite connections are not thread-shared.
        with Store(out / "state.db") as store:
            r = run_lesson(cfg, key, out, source=item.get("source"),
                           expected_duration=float(item.get("expected_duration") or 0),
                           store=store, force=bool(item.get("force")))
        return {"gate": "PASS" if r.gate_passed else "FAIL",
                "segments": r.meta.get("clean_segments", 0)}

    from .pool import recommended_workers
    workers = args.workers or recommended_workers(
        threads_per_worker=base.asr_threads)
    summary = run_pool(items, worker, workers=workers, log_dir=log_dir)
    print(f"batch   : {summary['ok']}/{summary['total']} ok, "
          f"{summary['seconds']}s, {workers} workers")
    print(f"summary : {log_dir / 'summary.json'}")
    return 0 if summary["ok"] == summary["total"] else 1


def cmd_distill_render(args: argparse.Namespace) -> int:
    """Re-project an existing distill.json. No audio, no ASR, no model."""
    from .distill.rerender import rerender

    lesson = Path(args.lesson).resolve()
    names = None
    if args.outputs:
        names = [x.strip() for x in args.outputs.split(",") if x.strip()]
    for name, path in rerender(lesson, _cfg(args), names, force=args.force):
        print(f"{name:11} {path}")
    return 0


def _report_usage(meta: dict) -> None:
    """Show what the run cost, and shout if the model was silently skipped."""
    dist = meta.get("distill") or {}
    if not dist.get("enabled"):
        return
    stats = dist.get("stats") or {}
    if dist.get("degraded"):
        print("warning : no model configured — fell back to rules, which cleans "
              "but does not condense. Run `v2n init` to set one up.")
    if stats.get("tokens_in") or stats.get("tokens_out"):
        cost = stats.get("cost")
        money = f"{cost:.4f}" if isinstance(cost, (int, float)) else "unknown (no price set)"
        print(f"tokens  : in={stats.get('tokens_in', 0)} "
              f"out={stats.get('tokens_out', 0)}  cost={money}")


def cmd_init(args: argparse.Namespace) -> int:
    """Store a model endpoint and key once, so later runs need no env vars."""
    from .provider import load_credentials, save_credentials

    interactive = sys.stdin.isatty()
    if not interactive and not (args.base_url and args.model):
        print("error: non-interactive; pass --base-url and --model", file=sys.stderr)
        return 2
    existing = load_credentials()
    base = args.base_url or input(f"API base URL [{existing.get('base_url', '')}]: ").strip()
    model = args.model or input(f"model name [{existing.get('model', '')}]: ").strip()
    api_key = args.api_key
    if api_key is None and interactive:
        api_key = input("API key (leave empty for local servers): ").strip()
    data = {
        "base_url": base or existing.get("base_url", ""),
        "model": model or existing.get("model", ""),
        "api_key": api_key if api_key is not None else existing.get("api_key", ""),
        "kind": args.kind,
        "price_in": args.price_in,
        "price_out": args.price_out,
    }
    path = save_credentials({k: v for k, v in data.items() if v not in ("", None)})
    print(f"saved   : {path}")
    print(f"model   : {data['model']} via {data['base_url']}")
    print("note    : stored in plain text, owner-readable only; "
          "keep it out of version control")
    return 0


def _timestamp(raw: str) -> float:
    """Accept ``90``, ``1:30`` or ``01:30`` and return seconds."""
    text = raw.strip()
    if not text:
        raise ValueError("empty timestamp")
    if ":" in text:
        parts = text.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        raise ValueError(f"bad timestamp: {raw!r}")
    return float(text)


def cmd_distill_repl(args: argparse.Namespace) -> int:
    """Re-distil one time window; every claim outside it is left untouched."""
    from .distill.partial import re_distil

    start, end = _timestamp(args.from_ts), _timestamp(args.to_ts)
    if end <= start:
        raise ValueError(f"--to ({end:.1f}s) must be greater than --from ({start:.1f}s)")
    names = None
    if args.outputs:
        names = [x.strip() for x in args.outputs.split(",") if x.strip()]
    lesson = Path(args.lesson).resolve()
    if not (lesson / "distill.json").exists():
        raise FileNotFoundError(f"{lesson / 'distill.json'} not found")
    meta = re_distil(lesson, _cfg(args), start=start, end=end, outputs=names)
    print(f"window  : {start:.1f}s – {end:.1f}s  ({meta['segments_in_window']} segments)")
    print(f"claims  : replaced {meta['claims_replaced']} -> added {meta['claims_added']}")
    cost = meta.get("cost")
    money = f"{cost:.4f}" if isinstance(cost, (int, float)) else "unknown"
    print(f"tokens  : in={meta.get('tokens_in', 0)} out={meta.get('tokens_out', 0)} "
          f"cost={money}")
    print(f"outputs : {', '.join(meta.get('outputs') or [])}")
    return 0


def cmd_agent_prepare(args: argparse.Namespace) -> int:
    """Write one work order per chunk for an external agent host to answer."""
    from .agent import load_segments, prepare

    lesson = Path(args.lesson).resolve()
    cfg = _cfg(args, work_dir=lesson)
    segments = load_segments(lesson, cfg)
    info = prepare(lesson, segments, chunk_size=args.chunk_size,
                   force=args.force, lesson_key=args.key or "")
    print(f"lesson  : {info['lesson_key']}")
    print(f"segments: {info['segments']}  chunk_size={info['chunk_size']}")
    print(f"chunks  : {info['chunks']}  submitted={info['submitted']} "
          f"pending={len(info['pending'])}")
    print(f"orders  : {lesson / '_agent'}")
    if info["pending"]:
        print("next    : answer each chunk, then "
              "`v2n agent submit --lesson DIR --chunk ID --file answers.json`")
    return 0


def cmd_agent_submit(args: argparse.Namespace) -> int:
    """Validate one chunk's answers; a re-submit replaces the previous result."""
    from .agent import load_segments, submit

    lesson = Path(args.lesson).resolve()
    cfg = _cfg(args, work_dir=lesson)
    segments = load_segments(lesson, cfg)
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    info = submit(lesson, args.chunk, raw, segments)
    counts = info["counts"]
    print(f"chunk    : {info['chunk']}")
    print(f"accepted : {info['accepted']}")
    print(f"rejected : {counts['rejected']} "
          f"(untraceable={counts['untraceable']} duplicates={counts['duplicates']})")
    for row in info["rejected"]:
        detail = row.get("text") or row.get("detail") or ""
        print(f"  - {row['reason']}: seg {row.get('seg', '?')} {detail}")
    print(f"progress : {info['submitted']}/{info['chunks']} chunks")
    if info["pending"]:
        print(f"pending  : {', '.join(info['pending'])}")
    else:
        print("next     : `v2n agent finalize --lesson DIR`")
    return 0 if info["accepted"] else 1


def cmd_agent_finalize(args: argparse.Namespace) -> int:
    """Assemble the IR from submitted chunks, write distill.json, render."""
    from .agent import finalize

    lesson = Path(args.lesson).resolve()
    cfg = _cfg(args, work_dir=lesson)
    names = ([x.strip() for x in args.outputs.split(",") if x.strip()]
             if args.outputs else None)
    meta = finalize(lesson, cfg, outputs=names)
    print(f"claims  : {meta['claims']} claims / {meta['topics']} topics, "
          f"coverage {float(meta['coverage']) * 100:.0f}%, "
          f"compression {float(meta['compression']):.2f} (agent)")
    print(f"dropped : {meta['untraceable']} untraceable, "
          f"{meta['duplicates']} duplicate")
    print(f"gate    : {meta['gate']}")
    for reason in meta["reasons"]:
        print(f"  - {reason}")
    print(f"outputs : {', '.join(meta['outputs']) or '(withheld — gate FAIL)'}")
    print(f"ir      : {lesson / 'distill.json'}")
    return 0 if meta["gate"] == "PASS" else 1


def cmd_agent_status(args: argparse.Namespace) -> int:
    from .agent import status

    info = status(Path(args.lesson).resolve())
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["ready"] else 1


def _hotkey_target(args: argparse.Namespace) -> Path:
    """Project file when a lesson is given, otherwise the user-wide file."""
    from .hotkeys import PROJECT_FILE, USER_PATH

    if getattr(args, "lesson", None):
        return Path(args.lesson).resolve() / PROJECT_FILE
    return USER_PATH


def cmd_hotkeys(args: argparse.Namespace) -> int:
    from .hotkeys import KINDS, add, as_mapping, disabled_phrases, load_hotkeys, remove

    if args.hotkeys_cmd == "list":
        lesson = Path(args.lesson).resolve() if args.lesson else None
        project_file = (lesson / "hotkeys.yaml") if lesson else None
        if project_file is not None and not project_file.exists():
            # A typo in --lesson would otherwise look like "this lesson has no
            # project hotkeys", which is a different answer.
            print(f"note    : {project_file} not found; showing builtin + user only")
        hotkeys = load_hotkeys(project=project_file)
        mapping = as_mapping(hotkeys)
        total = sum(len(v) for v in mapping.values())
        print(f"hotkeys : {total} 条")
        muted = disabled_phrases(project=project_file)
        if muted:
            print(f"  [muted] {len(muted)}: {', '.join(muted)}")
        for kind in KINDS:
            rows = mapping.get(kind) or []
            if not rows:
                continue
            print(f"  [{kind}] {len(rows)}")
            for row in rows:
                print(f"    {row['phrase']}  (w={row['weight']})")
        return 0

    if args.hotkeys_cmd == "add":
        target = _hotkey_target(args)
        add(target, args.phrase, kind=args.kind, weight=args.weight)
        print(f"added   : {args.phrase} -> {target}")
        return 0

    if args.hotkeys_cmd == "remove":
        from .hotkeys import USER_PATH
        from .hotkeys import builtin as builtin_hotkeys

        target = _hotkey_target(args)
        live = {h.phrase for h in (
            load_hotkeys(project=target) if target != USER_PATH
            else load_hotkeys(user=target))}
        if args.phrase not in live:
            # Saying "muted" for a phrase that never existed is the same class of
            # lie as reporting a skipped run as passed.
            print(f"not found: {args.phrase!r} is not an active hotkey in this tier")
            return 1
        was_builtin = any(h.phrase == args.phrase for h in builtin_hotkeys())
        remove(target, args.phrase)
        print(f"muted   : {args.phrase} -> {target}")
        if was_builtin:
            print("note    : builtin phrase; it is tombstoned in this tier and "
                  "stays off until you `add` it again")
        return 0

    raise ValueError(f"unknown hotkeys command: {args.hotkeys_cmd!r}")


def cmd_hotkeys_learn(args: argparse.Namespace) -> int:
    """Let the model propose project hotkeys from this lesson's transcript."""
    from .asr import load_transcript
    from .hotkeys import learn
    from .transcript import TermTable, clean

    lesson = Path(args.lesson).resolve()
    transcript = lesson / "transcript.txt"
    if not transcript.exists():
        raise FileNotFoundError(f"{transcript} not found")
    cfg = _cfg(args)
    segments = clean(load_transcript(transcript), TermTable.load(cfg.terms_file),
                     cfg).segments
    result = learn(segments, cfg, lesson_dir=lesson)
    print(f"proposed: {result['proposed']}  accepted: {result['accepted']}")
    print(f"tokens  : in={result.get('tokens_in', 0)} out={result.get('tokens_out', 0)}")
    for row in result["rejected"]:
        print(f"  dropped: {row['phrase']} ({row['reason']})")
    if result["path"]:
        print(f"written : {result['path']}")
        print("next    : re-run the lesson with --force, or "
              "`v2n distill render --lesson DIR` to refresh the output")
    return 0 if result["accepted"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    from .state import Store
    p = Path(args.db)
    if not p.exists():
        print(f"no state db: {p}")
        return 1
    with Store(p) as store:
        prog = store.progress()
        print(json.dumps(prog, ensure_ascii=False, indent=2))
        dead = store.dead_letters()
        if dead:
            print("dead letters:")
            for d in dead:
                print(f"  - {d['lesson_key']} / {d['stage']} / "
                      f"attempts={d['attempts']} / {str(d['last_error'])[:80]}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import importlib
    cfg = _cfg(args)
    print(f"video2notes {__version__}  python {sys.version.split()[0]}")
    print(f"config      : asr_backend={cfg.asr_backend} "
          f"threads={cfg.asr_threads} frames={cfg.frames_enabled}")
    for mod, extra in [("requests", "core"), ("yaml", "core"),
                       ("Crypto", "hls"), ("imageio_ffmpeg", "media"),
                       ("PIL", "media"), ("numpy", "media"),
                       ("faster_whisper", "asr"), ("pytest", "dev")]:
        try:
            importlib.import_module(mod)
            print(f"  {mod:18} OK      ({extra})")
        except ImportError:
            print(f"  {mod:18} MISSING (pip install -e '.[{extra}]')")
    ff = shutil.which("ffmpeg")
    print(f"  ffmpeg on PATH   : {ff or 'no (imageio-ffmpeg may still provide one)'}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import re

    from .prepublish import scan
    extra = [re.compile(p) for p in (args.pattern or [])]
    issues = scan(Path(args.root).resolve(), use_git=not args.no_git,
                  extra_patterns=extra)
    errors = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warn"]
    for i in issues:
        print(i)
    print(f"\nscan: {len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="v2n",
        description="Offline-first, resumable video-to-notes pipeline")
    ap.add_argument("--version", action="version", version=f"video2notes {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="YAML config; V2N_* env vars override it")

    d = sub.add_parser("demo", help="fully offline end-to-end run on synthetic audio")
    common(d)
    d.add_argument("--out", default="demo_out")
    d.add_argument("--seconds", type=float, default=60.0)
    d.add_argument("--outputs", help="distill outputs: comma list or 'all'")
    d.add_argument("--distiller", choices=["rules", "llm", "agent"])
    d.add_argument("--no-distill", action="store_true")
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("run", help="process one media source")
    common(r)
    r.add_argument("--source", required=True,
                   help="local path, direct media URL, or a media .m3u8 URL you may access")
    r.add_argument("--out", required=True)
    r.add_argument("--key", help="lesson key (defaults to output directory name)")
    r.add_argument("--asr", choices=["fake", "whisper"])
    r.add_argument("--lang", help="ASR language code, e.g. en or zh (default: config)")
    r.add_argument("--expected-duration", type=float, default=0.0,
                   help="reference duration in seconds; 0 = skip integrity check")
    r.add_argument("--frames", action="store_true")
    r.add_argument("--terms", help="YAML term table for ASR correction")
    r.add_argument("--threads", type=int)
    r.add_argument("--force", action="store_true", help="ignore resume and re-run")
    r.add_argument("--outputs", help="distill outputs: comma list or 'all'")
    r.add_argument("--distiller", choices=["rules", "llm", "agent"])
    r.add_argument("--no-distill", action="store_true")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("batch", help="process a YAML manifest with a worker pool")
    common(b)
    b.add_argument("--manifest", required=True)
    b.add_argument("--out-root", required=True)
    b.add_argument("--logs")
    b.add_argument("--workers", type=int)
    b.set_defaults(func=cmd_batch)

    s = sub.add_parser("status", help="inspect the SQLite state store")
    s.add_argument("--db", required=True)
    s.set_defaults(func=cmd_status)

    sub.add_parser("doctor", help="show optional dependency status").set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="store the model endpoint and key for later runs")
    i.add_argument("--base-url", help="OpenAI-compatible base URL, e.g. https://api.x.com/v1")
    i.add_argument("--model")
    i.add_argument("--api-key")
    i.add_argument("--kind", choices=["byok", "managed"], default="byok",
                   help="byok = your own key; managed = vendor gateway (reserved)")
    i.add_argument("--price-in", type=float, default=0.0, help="price per 1M input tokens")
    i.add_argument("--price-out", type=float, default=0.0)
    i.set_defaults(func=cmd_init)

    c = sub.add_parser("check", help="pre-publish safety scan")
    c.add_argument("--root", default=".")
    c.add_argument("--no-git", action="store_true",
                   help="walk the directory instead of using git ls-files")
    c.add_argument("--pattern", action="append", metavar="REGEX",
                   help="extra forbidden regex (repeatable), e.g. an internal codename")
    c.set_defaults(func=cmd_check)

    di = sub.add_parser("distill", help="inspect or re-render distillation output")
    di_sub = di.add_subparsers(dest="distill_cmd", required=True)
    dr = di_sub.add_parser("render", help="re-render outputs from an existing distill.json")
    common(dr)
    dr.add_argument("--lesson", required=True, help="lesson output directory")
    dr.add_argument("--outputs", help="comma list, or 'all'")
    dr.add_argument("--force", action="store_true",
                    help="render even if the stored IR failed the gate")
    dr.set_defaults(func=cmd_distill_render)

    rp = di_sub.add_parser("repl",
                           help="re-distil one time window, keeping the rest untouched")
    common(rp)
    rp.add_argument("--lesson", required=True, help="lesson output directory")
    rp.add_argument("--from", dest="from_ts", required=True,
                    help="window start, e.g. 3:20 or 200")
    rp.add_argument("--to", dest="to_ts", required=True,
                    help="window end, e.g. 6:00 or 360")
    rp.add_argument("--outputs", help="comma list, or 'all'")
    rp.set_defaults(func=cmd_distill_repl)

    hk = sub.add_parser("hotkeys", help="inspect or edit emphasis trigger phrases")
    hk_sub = hk.add_subparsers(dest="hotkeys_cmd", required=True)
    hl = hk_sub.add_parser("list", help="show merged builtin/project/user hotkeys")
    hl.add_argument("--lesson", help="include this lesson's project file")
    hl.set_defaults(func=cmd_hotkeys)
    ha = hk_sub.add_parser("add", help="add a phrase (project scope when --lesson given)")
    ha.add_argument("phrase")
    ha.add_argument("--kind", choices=["emphasis", "caution", "structure", "definition"],
                    default="emphasis")
    ha.add_argument("--weight", type=float)
    ha.add_argument("--lesson")
    ha.set_defaults(func=cmd_hotkeys)
    hr = hk_sub.add_parser("remove", help="remove a phrase")
    hr.add_argument("phrase")
    hr.add_argument("--lesson")
    hr.set_defaults(func=cmd_hotkeys)
    hn = hk_sub.add_parser("learn", help="ask the model for project hotkeys")
    common(hn)
    hn.add_argument("--lesson", required=True)
    hn.set_defaults(func=cmd_hotkeys_learn)

    ag = sub.add_parser("agent", help="hand distillation to an external agent host")
    ag_sub = ag.add_subparsers(dest="agent_cmd", required=True)
    ap_prep = ag_sub.add_parser("prepare", help="write one work order per chunk")
    common(ap_prep)
    ap_prep.add_argument("--lesson", required=True, help="lesson output directory")
    ap_prep.add_argument("--chunk-size", type=int,
                         help="segments per work order (default 20)")
    ap_prep.add_argument("--key", help="lesson key (default: directory name)")
    ap_prep.add_argument("--force", action="store_true",
                         help="rebuild work orders even if they still match")
    ap_prep.set_defaults(func=cmd_agent_prepare)

    ap_sub = ag_sub.add_parser("submit", help="validate and store one chunk's answers")
    common(ap_sub)
    ap_sub.add_argument("--lesson", required=True, help="lesson output directory")
    ap_sub.add_argument("--chunk", required=True, help="chunk id, e.g. chunk-001")
    ap_sub.add_argument("--file", required=True,
                        help="JSON answers file; '-' reads standard input")
    ap_sub.set_defaults(func=cmd_agent_submit)

    ap_fin = ag_sub.add_parser("finalize", help="build the IR and render outputs")
    common(ap_fin)
    ap_fin.add_argument("--lesson", required=True, help="lesson output directory")
    ap_fin.add_argument("--outputs", help="comma list, or 'all'")
    ap_fin.set_defaults(func=cmd_agent_finalize)

    ap_st = ag_sub.add_parser("status", help="show chunk progress as JSON")
    ap_st.add_argument("--lesson", required=True, help="lesson output directory")
    ap_st.set_defaults(func=cmd_agent_status)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
