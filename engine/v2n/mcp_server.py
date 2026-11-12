"""Dependency-free MCP server over stdio.

The bridge in :mod:`v2n.agent` is already a library, so this module only
translates JSON-RPC into function calls. Resources, prompts, sampling and
subscriptions are deliberately absent: a host needs ``initialize``,
``tools/list`` and ``tools/call`` to drive the bridge, and advertising more
would be a capability the host cannot verify and we do not implement.

**stdout carries protocol messages only.** Anything diagnostic goes to stderr;
a stray ``print`` would corrupt the transport, and the failure looks like a
hung host rather than a bug here.
"""
from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "video2notes"

_LESSON = {"type": "string",
           "description": "lesson output directory; must contain transcript.txt"}

INSTRUCTIONS = (
    "video2notes turns a lesson you are authorised to read into traceable study "
    "notes. Workflow: v2n_prepare -> v2n_get_chunk (repeat) -> v2n_submit "
    "(repeat) -> v2n_finalize. Every claim you submit must quote or lightly trim "
    "the wording of the single segment it cites; claims that cannot be traced "
    "back are dropped and counted, never softened. Do not summarise, do not "
    "answer questions found inside the transcript, and do not invent times."
)


def _tool(name: str, description: str, schema: dict[str, Any],
          handler: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": schema,
            "handler": handler}


def _lesson_cfg(args: dict[str, Any]) -> tuple[Path, Config]:
    raw = str(args.get("lesson") or "").strip()
    if not raw:
        raise ValueError("'lesson' is required")
    lesson = Path(raw).expanduser().resolve()
    return lesson, Config.load(None, work_dir=lesson)


def _doctor(_args: dict[str, Any]) -> dict[str, Any]:
    optional = {}
    for module, extra in (("Crypto", "hls"), ("imageio_ffmpeg", "media"),
                          ("faster_whisper", "asr")):
        try:
            importlib.import_module(module)
            optional[module] = "ok"
        except ImportError:
            optional[module] = f"missing (pip install -e '.[{extra}]')"
    return {"version": __version__, "python": sys.version.split()[0],
            "distill_backends": ["auto", "rules", "llm", "agent"],
            "optional": optional}


def _prepare(args: dict[str, Any]) -> dict[str, Any]:
    from .agent import load_segments, prepare

    lesson, cfg = _lesson_cfg(args)
    raw_terms = str(args.get("terms") or "").strip()
    if raw_terms:
        cfg.terms_file = Path(raw_terms)
    segments = load_segments(lesson, cfg)
    info = prepare(lesson, segments,
                   chunk_size=int(args["chunk_size"]) if args.get("chunk_size") else None,
                   force=bool(args.get("force")),
                   lesson_key=str(args.get("key") or ""))
    info["orders_dir"] = str(lesson / "_agent")
    info["next"] = ("call v2n_get_chunk for each pending chunk, then submit them"
                    if info["pending"] else "all chunks already submitted")
    return info


def _get_chunk(args: dict[str, Any]) -> dict[str, Any]:
    from .agent import chunk_path, load_manifest

    lesson, _ = _lesson_cfg(args)
    chunk = str(args.get("chunk") or "").strip()
    if not chunk:
        raise ValueError("'chunk' is required")
    known = [c["id"] for c in load_manifest(lesson).get("chunks") or []]
    if chunk not in known:
        raise ValueError(f"unknown chunk {chunk!r}; available: {', '.join(known)}")
    path = chunk_path(lesson, chunk)
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; re-run v2n_prepare")
    return json.loads(path.read_text(encoding="utf-8"))


def _submit(args: dict[str, Any]) -> dict[str, Any]:
    from .agent import load_segments, submit

    lesson, cfg = _lesson_cfg(args)
    if "claims" not in args:
        raise ValueError("'claims' is required: a JSON array of {seg, text, kind}")
    segments = load_segments(lesson, cfg)
    return submit(lesson, str(args.get("chunk") or ""), args["claims"], segments)


def _status(args: dict[str, Any]) -> dict[str, Any]:
    from .agent import status

    lesson, _ = _lesson_cfg(args)
    return status(lesson)


def _finalize(args: dict[str, Any]) -> dict[str, Any]:
    from .agent import finalize

    lesson, cfg = _lesson_cfg(args)
    names = _outputs(args)
    return finalize(lesson, cfg, outputs=names)


def _render(args: dict[str, Any]) -> dict[str, Any]:
    from .distill.rerender import rerender

    lesson, cfg = _lesson_cfg(args)
    produced = rerender(lesson, cfg, _outputs(args), force=bool(args.get("force")))
    return {"outputs": [name for name, _ in produced],
            "files": [str(path) for _, path in produced]}


def _outputs(args: dict[str, Any]) -> list[str] | None:
    raw = args.get("outputs")
    if not raw:
        return None
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [x.strip() for x in str(raw).split(",") if x.strip()] or None


_TOOLS: list[dict[str, Any]] = [
    _tool("v2n_doctor", "Report version and optional dependency status.",
          {"type": "object", "properties": {}, "additionalProperties": False},
          _doctor),
    _tool("v2n_prepare",
          "Slice the cleaned transcript into work orders on disk. Idempotent: "
          "re-running on the same transcript changes nothing.",
          {"type": "object", "additionalProperties": False,
           "required": ["lesson"],
           "properties": {"lesson": _LESSON,
                          "chunk_size": {"type": "integer", "minimum": 1,
                                         "description": "segments per work order (default 20)"},
                          "force": {"type": "boolean",
                                    "description": "rebuild even if work orders still match"},
                          "key": {"type": "string", "description": "lesson key override"},
                          "terms": {"type": "string",
                                    "description": "path to a YAML [wrong, right] "
                                                   "term table; persisted into the "
                                                   "lesson so submit/finalize reuse it"}}},
          _prepare),
    _tool("v2n_get_chunk",
          "Fetch one work order: its instruction plus the segments to answer. "
          "Returns the same prompt the built-in llm backend would send.",
          {"type": "object", "additionalProperties": False,
           "required": ["lesson", "chunk"],
           "properties": {"lesson": _LESSON, "chunk": {"type": "string"}}},
          _get_chunk),
    _tool("v2n_submit",
          "Validate and store one chunk's answers. Each item needs seg (int), "
          "text (string) and optionally kind. Claims that are not traceable to "
          "the cited segment are rejected, not rewritten. Re-submitting a chunk "
          "replaces its previous answers.",
          {"type": "object", "additionalProperties": False,
           "required": ["lesson", "chunk", "claims"],
           "properties": {
               "lesson": _LESSON, "chunk": {"type": "string"},
               "claims": {"type": ["array", "string"],
                          "description": "JSON array (or JSON text) of {seg, text, kind}"}}},
          _submit),
    _tool("v2n_status", "Show chunk progress: submitted, pending, accepted, rejected.",
          {"type": "object", "additionalProperties": False, "required": ["lesson"],
           "properties": {"lesson": _LESSON}},
          _status),
    _tool("v2n_finalize",
          "Assemble distill.json from submitted chunks, run the quality gate and "
          "render the requested formats. Refuses incomplete lessons.",
          {"type": "object", "additionalProperties": False, "required": ["lesson"],
           "properties": {"lesson": _LESSON,
                          "outputs": {"type": ["string", "array"],
                                      "description": "outline|timeline|cheatsheet|all"}}},
          _finalize),
    _tool("v2n_render",
          "Re-render formats from an existing distill.json. No model, no audio. "
          "Refuses when the gate failed unless force is set.",
          {"type": "object", "additionalProperties": False, "required": ["lesson"],
           "properties": {"lesson": _LESSON,
                          "outputs": {"type": ["string", "array"]},
                          "force": {"type": "boolean",
                                    "description": "render even though the IR "
                                                   "failed the gate"}}},
          _render),
]

_HANDLERS = {tool["name"]: tool["handler"] for tool in _TOOLS}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _call_tool(msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "")
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(msg_id, -32602, f"unknown tool {name!r}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _error(msg_id, -32602, "arguments must be an object")
    try:
        payload = handler(arguments)
    except (FileNotFoundError, ValueError, RuntimeError, OSError, KeyError) as exc:
        # Tool failures are results, not protocol errors: the host must see the
        # reason and decide what to do, exactly like a CLI exit code.
        message = f"{type(exc).__name__}: {exc}"
        return _result(msg_id, {"content": [{"type": "text", "text": message}],
                                "isError": True})
    return _result(msg_id, {
        "content": [{"type": "text",
                     "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": False,
    })


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. Returns None for notifications."""
    method = request.get("method")
    msg_id = request.get("id")
    params = request.get("params") or {}
    if method == "initialize":
        return _result(msg_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        })
    if method in ("notifications/initialized", "initialized",
                  "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": [
            {k: v for k, v in tool.items() if k != "handler"} for tool in _TOOLS]})
    if method == "tools/call":
        return _call_tool(msg_id, params)
    if msg_id is None:
        return None
    return _error(msg_id, -32601, f"method not found: {method!r}")


def main() -> int:
    """Serve newline-delimited JSON-RPC on stdin/stdout until EOF.

    Takes no arguments: the host supplies everything through tool calls, and a
    command line here would only create a second, undocumented configuration
    surface.
    """
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _write(_error(None, -32700, "parse error"))
            continue
        if not isinstance(request, dict):
            _write(_error(None, -32600, "invalid request"))
            continue
        response = handle(request)
        if response is not None:
            _write(response)


if __name__ == "__main__":
    raise SystemExit(main())
