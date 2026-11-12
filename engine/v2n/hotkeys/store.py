"""Load, merge and persist the three hotkey tiers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..atomic import atomic_write_text
from .model import KINDS, Hotkey, HotkeySet, merge

BUILTIN_PATH = Path(__file__).parent / "builtin.yaml"
USER_PATH = Path.home() / ".config" / "v2n" / "hotkeys.yaml"

_KIND_BY_KEY = {
    "emphasis": "emphasis", "latin_emphasis": "emphasis",
    "caution": "caution", "structure": "structure", "definition": "definition",
}


def _from_mapping(data: Any, source: str) -> HotkeySet:
    """Accept ``{kind: [phrases | {phrase, weight}]}`` and ``[{phrase, kind}]``.

    Both shapes must round-trip: :func:`save_hotkeys` writes the first, while a
    hand-edited file often uses the second. Reading back only bare strings
    silently dropped every entry the tool itself had written.
    """
    items: list[Hotkey] = []
    if isinstance(data, dict):
        for key, phrases in data.items():
            kind = _KIND_BY_KEY.get(str(key), "emphasis")
            for row in phrases or []:
                if isinstance(row, dict):
                    text = str(row.get("phrase", "")).strip()
                    weight = row.get("weight")
                    items.append(Hotkey(
                        text, kind=kind,
                        weight=float(weight) if weight is not None else None,
                        source=str(row.get("source", source))))
                else:
                    items.append(Hotkey(str(row).strip(), kind=kind, source=source))
    elif isinstance(data, list):
        for row in data:
            if isinstance(row, str):
                items.append(Hotkey(row.strip(), source=source))
            elif isinstance(row, dict) and row.get("phrase"):
                kind = str(row.get("kind", "emphasis"))
                items.append(Hotkey(
                    str(row["phrase"]).strip(),
                    kind=kind if kind in KINDS else "emphasis",
                    weight=(float(row["weight"]) if row.get("weight") is not None else None),
                    source=str(row.get("source", source)),
                ))
    return HotkeySet([h for h in items if h.phrase])


DISABLED_KEY = "disabled"


def _read_raw(path: Path | None) -> Any:
    if not path or not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _disabled_of(data: Any) -> tuple[str, ...]:
    """Phrases a tier has switched off.

    Without this a builtin phrase could never be muted: deleting it from a
    writable file looks like it worked, then the next merge pulls it straight
    back out of the builtin table. A tombstone is the only representation that
    survives merging.
    """
    if not isinstance(data, dict):
        return ()
    raw = data.get(DISABLED_KEY) or []
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(x).strip() for x in raw if str(x).strip())


def _strip_disabled(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k != DISABLED_KEY}
    return data


def _load_layer(path: Path | None, source: str) -> tuple[HotkeySet, tuple[str, ...]]:
    data = _read_raw(path)
    return _from_mapping(_strip_disabled(data), source), _disabled_of(data)


def _load_file(path: Path, source: str) -> HotkeySet:
    return _load_layer(path, source)[0]


def builtin() -> HotkeySet:
    return _load_file(BUILTIN_PATH, "builtin")


def _tiers(project: Path | None, user: Path | None) -> list[tuple[str, Path | None]]:
    return [("builtin", BUILTIN_PATH), ("project", project),
            ("user", user if user is not None else USER_PATH)]


def disabled_phrases(*, project: Path | None = None,
                     user: Path | None = None) -> tuple[str, ...]:
    seen: list[str] = []
    for _source, path in _tiers(project, user):
        for phrase in _load_layer(path, _source)[1]:
            if phrase not in seen:
                seen.append(phrase)
    return tuple(seen)


def load_hotkeys(*, project: Path | None = None,
                 user: Path | None = None) -> HotkeySet:
    """Builtin < project < user. A missing file is simply an empty tier."""
    sets = []
    blocked: set[str] = set()
    for source, path in _tiers(project, user):
        layer, disabled = _load_layer(path, source)
        sets.append(layer)
        blocked.update(disabled)
    merged = merge(*sets)
    if not blocked:
        return merged
    return HotkeySet([h for h in merged if h.phrase not in blocked])


def as_mapping(hotkeys: HotkeySet) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for hotkey in hotkeys:
        grouped.setdefault(hotkey.kind, []).append(
            {"phrase": hotkey.phrase, "weight": round(hotkey.effective_weight, 3)})
    return grouped


def save_hotkeys(hotkeys: HotkeySet, path: Path,
                 *, disabled: tuple[str, ...] | None = None) -> Path:
    """Write one tier. Existing tombstones are preserved unless overridden.

    ``disabled=None`` means "keep whatever this file already had": callers such
    as the learning pass rewrite the phrase list and would otherwise wipe the
    muted set as a side effect.
    """
    mapping: dict[str, Any] = dict(as_mapping(hotkeys))
    if disabled is None:
        disabled = _disabled_of(_read_raw(path))
    if disabled:
        mapping[DISABLED_KEY] = sorted(set(disabled))
    return atomic_write_text(
        Path(path), yaml.safe_dump(mapping, allow_unicode=True, sort_keys=True))


def add(path: Path, phrase: str, *, kind: str = "emphasis",
        weight: float | None = None) -> HotkeySet:
    """Add or override one phrase in a writable tier file.

    Re-adding a muted phrase un-mutes it: an explicit add outranks an older
    tombstone, otherwise the command would silently do nothing.
    """
    text = phrase.strip()
    existing, disabled = _load_layer(path, "project")
    fresh = HotkeySet([h for h in existing if h.phrase != text]
                      + [Hotkey(text, kind=kind, weight=weight,
                                source="project")])
    save_hotkeys(fresh, path, disabled=tuple(d for d in disabled if d != text))
    return fresh


def remove(path: Path, phrase: str) -> HotkeySet:
    """Mute a phrase in this tier, including one that comes from builtin.

    Physical deletion is not enough: the builtin table is re-merged on every
    load, so the phrase would reappear on the next command. Writing a tombstone
    is what makes "remove" mean what the user expects.
    """
    text = phrase.strip()
    existing, disabled = _load_layer(path, "project")
    kept = HotkeySet([h for h in existing if h.phrase != text])
    save_hotkeys(kept, path, disabled=tuple(sorted({*disabled, text})))
    return kept
