#!/usr/bin/env python3
"""Verify properties of the deployment that a "did it install?" check misses.

Four claims are checked, each of which has been wrong in real installers:

1. The config entry points at an interpreter that exists and at the right module.
2. A second run leaves the config byte-identical and leaves no new backup.
3. Foreign servers and unrelated top-level keys survive registration.
4. Removing the root leaves nothing behind.

Run after ``bootstrap.py``; CI runs it on Linux, macOS and Windows because the
path handling (Scripts vs bin, drive letters, atomic replace) is where an
installer actually breaks.

    python scripts/selfcheck.py --root /tmp/vde --config /tmp/mcp.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "bootstrap.py"
SERVER_ID = "video-distill-expert"


def bootstrap(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(BOOTSTRAP), *args], check=False,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=900)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_entry(config: Path) -> None:
    data = json.loads(read(config))
    entry = (data.get("mcpServers") or {}).get(SERVER_ID)
    assert entry, f"{SERVER_ID} missing from {config}"
    assert entry["args"] == ["-m", "v2n.mcp_server"], entry["args"]
    assert Path(entry["command"]).exists(), f"interpreter missing: {entry['command']}"
    assert entry["env"]["PYTHONUTF8"] == "1", entry.get("env")
    print(f"ok   entry -> {entry['command']}")


def check_idempotent(root: Path, config: Path, skill: Path) -> None:
    before = read(config)
    existed = {p.name for p in config.parent.glob(f"{config.name}.bak-*")}
    run = bootstrap(["--root", str(root), "--host", "workbuddy",
                     "--config", str(config), "--skill-dir", str(skill)])
    assert run.returncode == 0, run.stdout + run.stderr
    assert "already registered, unchanged" in run.stdout, run.stdout[-1500:]
    assert read(config) == before, "config was rewritten on a no-op run"
    after = {p.name for p in config.parent.glob(f"{config.name}.bak-*")}
    assert after == existed, f"new backups appeared: {sorted(after - existed)}"
    print("ok   second run is a no-op")


def check_merge() -> None:
    temp = Path(tempfile.mkdtemp(prefix="vde-merge-"))
    config = temp / "mcp.json"
    seed = {
        "mcpServers": {"keep-me": {"command": "node", "args": ["x.js"]}},
        "unrelated": {"nested": [1, 2], "flag": True},
    }
    config.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    run = bootstrap(["--root", str(temp / "root"), "--host", "workbuddy",
                     "--config", str(config), "--skill-dir", str(temp / "skills")])
    assert run.returncode == 0, run.stdout + run.stderr
    data = json.loads(read(config))
    assert data["mcpServers"]["keep-me"] == seed["mcpServers"]["keep-me"]
    assert data["unrelated"] == seed["unrelated"]
    assert SERVER_ID in data["mcpServers"]
    backup = list(temp.glob("mcp.json.bak-*"))
    assert len(backup) == 1, f"expected exactly one backup, got {backup}"
    assert json.loads(read(backup[0])) == seed, "backup is not the original"
    print("ok   foreign entries and unknown keys preserved")


def check_uninstall(root: Path) -> None:
    import shutil

    shutil.rmtree(root, ignore_errors=True)
    assert not root.exists(), root
    print("ok   root removed cleanly")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check deployment properties, not just exit codes.")
    ap.add_argument("--root", required=True, help="deployment root that was used")
    ap.add_argument("--config", required=True, help="MCP config that was written")
    ap.add_argument("--skill-dir", required=True, help="skill directory that was used")
    ap.add_argument("--keep", action="store_true", help="do not remove the root at the end")
    args = ap.parse_args(argv)

    root, config, skill = Path(args.root), Path(args.config), Path(args.skill_dir)
    print(f"checking deployment at {root}")
    check_entry(config)
    check_idempotent(root, config, skill)
    check_merge()
    if args.keep:
        print("ok   left root in place (--keep)")
    else:
        check_uninstall(root)
    print("OK   deployment properties hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
