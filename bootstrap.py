#!/usr/bin/env python3
"""One-command deployment for 视频蒸馏专家 (video-distill-expert).

Why this file exists rather than a ``curl | bash`` one-liner: the deployment has
to work on Windows PowerShell, macOS and Linux, be readable before it is run,
and leave an existing MCP configuration untouched. A shell script can do at most
one of those.

Design commitments, in the order they constrain the code:

* **Standard library only.** The installer cannot depend on the thing it installs.
* **Idempotent.** Running it twice must not create a second venv, rewrite an
  identical config, or accumulate ``.bak`` files. Users re-run installers.
* **Non-destructive.** Other MCP servers in the same file are preserved verbatim;
  the file is backed up before any write and replaced atomically.
* **Self-verifying.** Installing files proves nothing. This script ends with a
  real JSON-RPC handshake against the installed server and reports what the host
  will actually see.

Exit codes are part of the contract (``AGENTS.md`` branches on them):
0 success, 2 bad environment/arguments, 3 install failure, 4 probe failure.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

MIN_PYTHON = (3, 10)
SERVER_ID = "video-distill-expert"
DEFAULT_ROOT = Path.home() / ".video-distill-expert"
REPO_ROOT = Path(__file__).resolve().parent
ENGINE_DIR = REPO_ROOT / "engine"
SKILL_SRC = REPO_ROOT / "skill" / "SKILL.md"

EXIT_OK = 0
EXIT_ENV = 2
EXIT_INSTALL = 3
EXIT_PROBE = 4

# The banner is Chinese, and a legacy Windows console code page is not: on an
# en-US machine stdout is cp1252, which cannot encode 视, so the very first
# print dies with UnicodeEncodeError before a single real step runs. Windows
# runners are exactly such machines — which is how CI caught this and my
# zh-CN laptop did not. `errors="replace"` keeps even a hostile encoding from
# raising; the venv's children get the same guard via env vars in run().
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Host-specific locations. ``generic`` prints a snippet instead of writing,
# because guessing a path for an unknown host is how configurations get corrupted.
HOSTS = {
    "workbuddy": {
        "config": Path.home() / ".workbuddy" / "mcp.json",
        "skill": Path.home() / ".workbuddy" / "skills" / SERVER_ID,
    },
    "claude": {
        "config": (Path(os.environ.get("APPDATA", Path.home())) / "Claude"
                   / "claude_desktop_config.json") if os.name == "nt" else (
                       Path.home() / "Library" / "Application Support" / "Claude"
                       / "claude_desktop_config.json"),
        "skill": Path.home() / ".claude" / "skills" / SERVER_ID,
    },
    "cursor": {
        "config": Path.home() / ".cursor" / "mcp.json",
        "skill": Path.home() / ".cursor" / "skills" / SERVER_ID,
    },
    "generic": {"config": None, "skill": None},
}


def log(msg: str) -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #

def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        log(f"FAIL python {sys.version.split()[0]} is too old; "
            f"need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")
        log("     install a newer Python, then re-run this script with it")
        raise SystemExit(EXIT_ENV)
    log(f"ok   python {sys.version.split()[0]} on {platform.system().lower()}")


def venv_python(venv: Path) -> Path:
    """Path to the interpreter inside a venv, per platform."""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # UTF-8 everywhere: the engine writes Chinese notes, and a legacy Windows
    # console code page turns that into a UnicodeEncodeError far from the cause.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(cmd, check=False, capture_output=True, text=True,
                          timeout=timeout,
                          encoding="utf-8", errors="replace", env=env)


def ensure_venv(root: Path, dry: bool) -> Path:
    venv = root / "venv"
    py = venv_python(venv)
    if py.exists():
        log(f"ok   reuse venv {venv}")
        return py
    if dry:
        log(f"plan create venv {venv}")
        return py
    step(f"creating venv at {venv}")
    root.mkdir(parents=True, exist_ok=True)
    result = run([sys.executable, "-m", "venv", str(venv)])
    if result.returncode != 0 or not py.exists():
        log("FAIL could not create a virtual environment")
        log((result.stderr or result.stdout).strip()[-1200:])
        log("     on Debian/Ubuntu this usually means: apt install python3-venv")
        raise SystemExit(EXIT_INSTALL)
    return py


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #

def _pip(py: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return run([str(py), "-m", "pip", "install", "--disable-pip-version-check",
                "--no-input", *args])


def install_engine(py: Path, *, editable: bool, dry: bool) -> None:
    target = str(ENGINE_DIR)
    mode = "-e" if editable else ""
    if dry:
        log(f"plan pip install {'-e ' if editable else ''}{target}")
        return
    step("installing the engine")
    cmd = [target] if not mode else [mode, target]
    result = _pip(py, cmd)
    if result.returncode == 0:
        log("ok   engine installed")
        return

    # Fallback: no network, no build backend. Putting the package on the venv's
    # path needs neither, and the distillation path genuinely works from source.
    # Dependencies are a separate problem, reported honestly rather than papered
    # over with a success message.
    log("warn pip install failed; falling back to a source-path link")
    log("     " + (result.stderr or result.stdout).strip().splitlines()[-1][:300])
    venv_root = py.parent.parent
    candidates = [*sorted(venv_root.glob("lib/python*/site-packages")),
                  venv_root / "Lib" / "site-packages"]
    site = next((p for p in candidates if p.exists()), None)
    if site is None:
        log("FAIL could not locate site-packages; cannot fall back")
        raise SystemExit(EXIT_INSTALL)
    (site / f"{SERVER_ID}.pth").write_text(str(ENGINE_DIR) + "\n", encoding="utf-8")
    if _pip(py, ["PyYAML>=6.0", "requests>=2.31"]).returncode != 0:
        log("FAIL dependencies are missing and could not be installed")
        log("     offline install: pip download PyYAML requests -d wheels/")
        log("     then re-run:     python bootstrap.py --wheels wheels/")
        raise SystemExit(EXIT_INSTALL)
    log("ok   linked from source with dependencies installed")


def verify_import(py: Path, dry: bool) -> str:
    if dry:
        return "?"
    step("checking the installed package")
    result = run([str(py), "-c",
                  "import v2n, v2n.agent, v2n.mcp_server; print(v2n.__version__)"])
    if result.returncode != 0:
        log("FAIL the package does not import")
        log((result.stderr or result.stdout).strip()[-1200:])
        raise SystemExit(EXIT_INSTALL)
    version = (result.stdout or "").strip().splitlines()[-1]
    log(f"ok   v2n {version} imports cleanly")
    return version


# --------------------------------------------------------------------------- #
# MCP registration
# --------------------------------------------------------------------------- #

def server_entry(py: Path) -> dict[str, object]:
    return {
        "command": str(py),
        "args": ["-m", "v2n.mcp_server"],
        "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    }


def _load_config(path: Path) -> tuple[dict[str, object], str]:
    """Return (config, note). A corrupt file is preserved, not silently replaced."""
    if not path.exists():
        return {"mcpServers": {}}, "new file"
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {"mcpServers": {}}, "new file"
    try:
        data = json.loads(raw)
    except ValueError:
        return {"mcpServers": {}}, "unparseable, will be backed up and rewritten"
    if not isinstance(data, dict):
        return {"mcpServers": {}}, "not an object, will be backed up and rewritten"
    return data, "merged"


def register_mcp(host: str, py: Path, *, dry: bool,
                 override: Path | None = None) -> str:
    target = override or HOSTS[host]["config"]
    entry = server_entry(py)
    if target is None:
        step("MCP configuration for a generic host")
        log(json.dumps({"mcpServers": {SERVER_ID: entry}}, indent=2,
                       ensure_ascii=False))
        log("\nAdd that block to your host's MCP config, then restart the host.")
        return "printed"

    step(f"registering the MCP server in {target}")
    if dry:
        log(f"plan merge mcpServers.{SERVER_ID} into {target}")
        return "planned"

    config, note = _load_config(target)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        config["mcpServers"] = servers

    previous = servers.get(SERVER_ID)
    # Compare, do not assume. Rewriting an identical file on every run makes the
    # timestamp meaningless and leaves the user wondering what changed.
    if previous == entry:
        log("ok   already registered, unchanged")
        return "unchanged"

    if target.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        shutil.copy2(target, backup)
        log(f"ok   backed up to {backup.name}")

    servers[SERVER_ID] = entry
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(target)  # atomic: a crash never leaves a half-written config
    log(f"ok   registered ({note}); {len(servers)} server(s) total")
    return "registered"


def install_skill(host: str, *, dry: bool, override: Path | None = None) -> str:
    dest = override or HOSTS[host]["skill"]
    if dest is None or not SKILL_SRC.exists():
        if not SKILL_SRC.exists():
            log("warn skill/SKILL.md is missing; skipping")
        else:
            log(f"note skill stays at {SKILL_SRC} for a generic host")
        return "skipped"
    target = dest / "SKILL.md"
    if dry:
        log(f"plan copy skill to {target}")
        return "planned"
    step(f"installing the skill at {dest}")
    if target.exists() and target.read_bytes() == SKILL_SRC.read_bytes():
        log("ok   already up to date")
        return "unchanged"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_SRC, target)
    log("ok   skill installed")
    return "installed"


# --------------------------------------------------------------------------- #
# probe: the only evidence that the deployment works
# --------------------------------------------------------------------------- #

def probe(py: Path, *, dry: bool) -> dict[str, object]:
    if dry:
        log("plan handshake with the installed server")
        return {}
    step("handshaking with the installed server")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "bootstrap", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "v2n_doctor", "arguments": {}}},
    ]
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([str(py), "-m", "v2n.mcp_server"], input=payload, check=False,
                              capture_output=True, text=True, timeout=90,
                              encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        log("FAIL the server did not answer within 90s")
        raise SystemExit(EXIT_PROBE) from None

    replies: dict[int, dict[str, object]] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            log(f"FAIL non-JSON on stdout: {line[:200]}")
            raise SystemExit(EXIT_PROBE) from None
        if isinstance(msg, dict) and isinstance(msg.get("id"), int):
            replies[msg["id"]] = msg

    if 1 not in replies or "result" not in replies[1]:
        log("FAIL initialize got no result")
        log((proc.stderr or proc.stdout).strip()[-800:])
        raise SystemExit(EXIT_PROBE)

    info = replies[1]["result"]
    tools = [t["name"] for t in replies.get(2, {}).get("result", {}).get("tools", [])]
    doctor = replies.get(3, {}).get("result", {}).get("structuredContent", {})
    if not tools:
        log("FAIL the server advertised no tools")
        raise SystemExit(EXIT_PROBE)

    log(f"ok   {info['serverInfo']['name']} {info['serverInfo']['version']} "
        f"@ protocol {info['protocolVersion']}")
    log(f"ok   {len(tools)} tools: {', '.join(tools)}")
    optional = doctor.get("optional") or {}
    missing = [k for k, v in optional.items() if v != "ok"]
    if missing:
        log(f"note optional extras not installed: {', '.join(missing)} "
            "(not needed to distil a transcript)")
    return {"tools": tools, "server": info["serverInfo"], "doctor": doctor}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Deploy 视频蒸馏专家 as an MCP server in one command.")
    ap.add_argument("--host", default="workbuddy", choices=sorted(HOSTS),
                    help="which MCP host to configure (default: workbuddy)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT),
                    help=f"where the venv lives (default: {DEFAULT_ROOT})")
    ap.add_argument("--editable", action="store_true",
                    help="install from the source tree instead of a built copy")
    ap.add_argument("--skill", action=argparse.BooleanOptionalAction, default=True,
                    help="also install the skill file (default: yes)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan without touching anything")
    ap.add_argument("--config", default=None,
                    help="override the MCP config path (mostly for testing)")
    ap.add_argument("--skill-dir", default=None,
                    help="override the skill destination directory")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable summary on the last line")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    host = args.host
    log(f"视频蒸馏专家 · deployment ({'dry run' if args.dry_run else 'live'})")
    log(f"root {root}")

    if not (ENGINE_DIR / "v2n" / "mcp_server.py").exists():
        log(f"FAIL engine not found at {ENGINE_DIR}")
        log("     run this script from a full checkout of the repository")
        return EXIT_ENV

    check_python()
    py = ensure_venv(root, args.dry_run)
    install_engine(py, editable=args.editable, dry=args.dry_run)
    version = verify_import(py, args.dry_run)
    cfg_override = Path(args.config).expanduser().resolve() if args.config else None
    skill_override = Path(args.skill_dir).expanduser().resolve() if args.skill_dir else None
    register_mcp(host, py, dry=args.dry_run, override=cfg_override)
    if args.skill:
        install_skill(host, dry=args.dry_run, override=skill_override)
    probe_info = probe(py, dry=args.dry_run)

    step("done")
    if args.dry_run:
        log("Dry run only. Re-run without --dry-run to apply.")
        return EXIT_OK

    config_path = cfg_override or HOSTS[host]["config"]
    log(f"engine    {py}")
    if config_path:
        log(f"config    {config_path}")
    log(f"skill     {skill_override or HOSTS[host]['skill'] or SKILL_SRC}")
    log("")
    log("Next:")
    if host != "generic":
        log("  1. Restart the host so it loads the new server.")
        log(f"  2. Ask it to run v2n_doctor — expect version {version} and "
            f"{len(probe_info.get('tools') or [])} tools.")
        log("  3. Point it at a lesson directory containing transcript.txt.")
    else:
        log("  1. Add the printed block to your host's MCP config.")
        log("  2. Restart the host and run v2n_doctor.")
    log("")
    log("Uninstall: delete the directory above and the "
        f"'{SERVER_ID}' entry in the MCP config.")

    if args.json:
        print(json.dumps({"ok": True, "root": str(root), "host": host,
                          "python": str(py), "version": version,
                          "config": str(config_path) if config_path else None,
                          "tools": probe_info.get("tools") or []},
                         ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
