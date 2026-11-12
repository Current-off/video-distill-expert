"""Hotkeys: phrase triggers that mark what a speaker treats as important.

Three tiers — builtin, project (model-written), user — merged by precedence and
applied to claims as an emphasis flag plus a small salience boost. Nothing here
can add content to a claim; it only decides which existing claims to point at.
"""
from __future__ import annotations

from .apply import apply
from .learn import PROJECT_FILE, learn, validate
from .model import KINDS, WEIGHTS, Hotkey, HotkeySet, merge
from .store import (
    BUILTIN_PATH,
    USER_PATH,
    add,
    as_mapping,
    builtin,
    disabled_phrases,
    load_hotkeys,
    remove,
    save_hotkeys,
)

__all__ = [
    "BUILTIN_PATH",
    "KINDS",
    "PROJECT_FILE",
    "USER_PATH",
    "WEIGHTS",
    "Hotkey",
    "HotkeySet",
    "add",
    "apply",
    "as_mapping",
    "builtin",
    "disabled_phrases",
    "learn",
    "load_hotkeys",
    "merge",
    "remove",
    "save_hotkeys",
    "validate",
]
