"""Pre-publish safety scan.

A video-processing project is one ``git add -A`` away from publishing somebody
else's media, a signed URL, or a cookie. This module fails loudly on those
patterns before anything leaves the machine.

The file set comes from ``git ls-files`` (cached + untracked, minus ignored) so
the check matches what git would actually commit. Outside a git repository it
falls back to a directory walk that skips generated-output directories.

The checker deliberately errs on the side of false positives: whitelisting a
path is cheap, leaking a credential is not.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".venv", "venv",
    "build", "dist", ".idea", ".vscode", "htmlcov", ".orchestrator",
}
SKIP_PATH_PARTS = {"demo_out", "out", "_out", "_logs", "frames"}
# These files contain the forbidden literals *as detection rules / test fixtures*.
SCAN_EXEMPT_FILES = {
    "v2n/prepublish.py",
    "scripts/prepublish_check.py",
    "tests/test_prepublish.py",
    # .gitignore legitimately lists patterns like *sessionid* so those files
    # are never committed; flagging the pattern itself would be a false positive.
    ".gitignore",
}
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_BINARY_BYTES = 2 * 1024 * 1024
BAD_EXTENSIONS = {
    ".ts", ".mp4", ".mkv", ".webm", ".mov", ".wav", ".mp3", ".m4a",
    ".jpg", ".jpeg", ".png", ".gif", ".db", ".db-wal", ".db-shm",
    ".log", ".part", ".tmp", ".pem", ".key", ".p12", ".pfx",
}

PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("credential", re.compile(
        r"(?i)\b(sessionid|csrftoken|set-cookie|authorization\s*:|"
        r"bearer\s+[A-Za-z0-9._\-]{20,}|AKIA[0-9A-Z]{16}|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"), "possible credential or token"),
    ("media_url", re.compile(
        r"(?i)https?://[^\s\"'<>]+\.m3u8(?:\?[^\s\"'<>]*)?"),
     "hard-coded HLS playlist URL"),
    ("private_key_assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*"
        r"['\"][A-Za-z0-9+/_\-]{16,}['\"]"),
     "hard-coded secret assignment"),
    # (?<![A-Za-z0-9]) prevents "https://..." from being read as drive "s:".
    ("windows_abs_path", re.compile(
        r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1,2}[^\s\"'<>]{8,}"),
     "Windows absolute path (machine-specific)"),
    ("posix_home_path", re.compile(
        r"(?:/Users/|/home/)[A-Za-z0-9._-]+/[^\s\"'<>]{4,}"),
     "POSIX home path (machine-specific)"),
]

# Organisation-specific markers (internal host names, project codenames) are
# intentionally NOT built in: they are meaningless to other users and would
# leak where the code came from. Pass them at runtime with `--pattern REGEX`
# or `scan(..., extra_patterns=[re.compile(...)])`.


@dataclass(frozen=True)
class Issue:
    level: str          # error | warn
    path: str
    line: int
    code: str
    detail: str

    def __str__(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{self.level.upper()}] {self.code} @ {loc} — {self.detail}"


def _git_files(root: Path) -> list[Path] | None:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(root), capture_output=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = proc.stdout.decode("utf-8", errors="replace")
    return [root / p for p in raw.split("\0") if p]


def _walk_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        parts = set(p.relative_to(root).parts)
        if parts & SKIP_DIRS or parts & SKIP_PATH_PARTS:
            continue
        if any(part.endswith(".egg-info") for part in parts):
            continue
        out.append(p)
    return out


def scan(root: Path, *, use_git: bool = True,
         extra_patterns: list[re.Pattern] | None = None) -> list[Issue]:
    """Return all issues; callers decide whether warnings block a release."""
    issues: list[Issue] = []
    root = Path(root).resolve()

    if not (root / ".gitignore").exists():
        issues.append(Issue("error", ".gitignore", 0, "missing_gitignore",
                            "a video project without .gitignore will commit media"))
    lic = root / "LICENSE"
    if lic.exists() and "<YOUR_NAME_OR_GITHUB_HANDLE>" in lic.read_text(encoding="utf-8"):
        issues.append(Issue("warn", "LICENSE", 0, "license_placeholder",
                            "replace the copyright placeholder before publishing"))

    files = _git_files(root) if use_git else None
    # An empty git repository legitimately returns []; `or` would conflate
    # that with "git unavailable" and silently switch to a directory walk.
    if files is None:
        files = _walk_files(root)
    for p in files:
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix() if p.is_relative_to(root) else p.as_posix()
        if rel in SCAN_EXEMPT_FILES:
            continue
        size = p.stat().st_size
        if p.suffix.lower() in BAD_EXTENSIONS:
            issues.append(Issue("error", rel, 0, "generated_artifact",
                                f"{p.suffix} file ({size} bytes) must not be committed"))
        if size > MAX_BINARY_BYTES:
            issues.append(Issue("warn", rel, 0, "large_file",
                                f"{size} bytes; consider Git LFS or removal"))
        if size > MAX_TEXT_BYTES or p.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".db", ".wav", ".mp4", ".ts", ".pyc"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for code, pat, detail in PATTERNS:
                if pat.search(line):
                    issues.append(Issue("error", rel, i, code, detail))
            for pat in extra_patterns or []:
                if pat.search(line):
                    issues.append(Issue("error", rel, i, "extra_pattern",
                                        "matched a user-supplied forbidden pattern"))
    return issues
