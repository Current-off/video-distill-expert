"""Configuration: dataclass defaults -> YAML file -> ``V2N_*`` environment.

There is deliberately **no** hard-coded host, path, account or catalogue in this
module. Every machine-specific value is supplied by the caller.

Precedence (later wins): dataclass defaults, YAML, environment variables,
explicit keyword arguments to :meth:`Config.load`.
"""
from __future__ import annotations

import dataclasses
import os
import warnings
from pathlib import Path
from typing import Any

import yaml


@dataclasses.dataclass
class Config:
    # --- output ---
    work_dir: Path = Path("out")

    # --- ASR ---
    asr_backend: str = "fake"          # fake | whisper
    asr_model: str = "small"
    asr_device: str = "cpu"            # cpu | cuda | auto
    asr_compute_type: str = "int8"
    asr_language: str = "zh"
    asr_threads: int = 4
    hotwords: list[str] = dataclasses.field(default_factory=list)

    # --- audio contract ---
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2              # bytes, pcm_s16le
    duration_tolerance: float = 0.02   # 2%

    # --- network ---
    http_timeout: float = 60.0
    http_attempts: int = 4
    qps: float = 0.0                   # 0 = unlimited
    referer: str = ""                  # empty = no Referer header
    user_agent: str = "video2notes/0.1.0"
    use_system_proxy: bool = True

    # --- transcript cleanup ---
    terms_file: Path | None = None     # YAML list of [wrong, right] pairs
    drop_hallucinations: bool = True
    merge_short_segments: bool = True
    merge_max_chars: int = 120
    # Never merge across a silence this long: the gap is the sentence boundary
    # that survives Chinese ASR, and character-count merging destroys it.
    clean_merge_max_gap: float = 0.6

    # --- distillation ---
    # `outputs` selects the projection of distill.json; the IR itself is always
    # built in full, so choosing more formats cannot lower the quality bar.
    distill_enabled: bool = True
    distill_outputs: list[str] = dataclasses.field(default_factory=lambda: ["outline"])
    distill_min_segment_chars: int = 6
    distill_max_claims_per_topic: int = 4
    # Silence (seconds) between ASR segments that closes a sentence. This is the
    # boundary signal that does not depend on the model emitting punctuation.
    distill_sentence_pause: float = 0.6
    # `auto` (default): use the model when one is configured, otherwise fall back
    # to `rules` and say so loudly. `rules` is offline and deterministic; `llm`
    # rewrites clauses into atomic assertions and is what the product is for.
    distill_backend: str = "auto"
    distill_llm_base_url: str = ""       # OpenAI-compatible; empty = not configured
    distill_llm_model: str = ""
    distill_llm_key_env: str = "V2N_LLM_API_KEY"
    distill_llm_timeout: float = 60.0
    distill_llm_batch: int = 20          # segments per request; keeps tasks small
    # Cost reporting. Per 1M tokens, in the currency you bill in. Left at 0 the
    # run reports "unknown" instead of pretending to be free.
    distill_llm_price_in: float = 0.0
    distill_llm_price_out: float = 0.0

    # --- hotkeys (emphasis triggers) ---
    hotkeys_enabled: bool = True
    hotkeys_user_file: Path | None = None   # None = ~/.config/v2n/hotkeys.yaml

    # --- frames ---
    frames_enabled: bool = False
    frame_interval: float = 30.0       # seconds
    dedupe_threshold: int = 8          # dHash Hamming distance

    # --- state ---
    state_db: Path = Path("state.db")
    lease_sec: int = 1800
    max_attempts: int = 3

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: Path | str | None = None, **overrides: Any) -> Config:
        data: dict[str, Any] = {}
        if path:
            p = Path(path)
            if p.exists():
                parsed = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if not isinstance(parsed, dict):
                    raise ValueError(f"config must be a mapping: {p}")
                data.update(parsed)

        # V2N_<FIELD_NAME> overrides, typed by the dataclass field.
        for f in dataclasses.fields(cls):
            env_key = "V2N_" + f.name.upper()
            if env_key in os.environ:
                data[f.name] = _coerce(os.environ[env_key], f)

        data.update(overrides)
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            # A typo such as `asr_backned` must not silently fall back to a
            # default. Warn rather than raise so extra/comment keys stay usable.
            warnings.warn(f"unknown config key(s), ignored: {', '.join(unknown)}",
                          UserWarning, stacklevel=2)
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        return cfg._normalise()

    def _normalise(self) -> Config:
        for f in dataclasses.fields(self):
            if f.name in ("work_dir", "state_db", "terms_file", "hotkeys_user_file"):
                val = getattr(self, f.name)
                if val is not None and not isinstance(val, Path):
                    setattr(self, f.name, Path(str(val)))
        # An empty YAML value means "not configured", not "current directory".
        if self.terms_file is not None and str(self.terms_file) in ("", "."):
            self.terms_file = None
        if self.asr_backend not in ("fake", "whisper"):
            raise ValueError(f"unknown asr_backend: {self.asr_backend!r} (fake|whisper)")
        if self.asr_threads < 1:
            raise ValueError("asr_threads must be >= 1")
        for name in ("sample_rate", "channels", "sample_width", "http_timeout",
                     "frame_interval", "lease_sec", "max_attempts",
                     "merge_max_chars", "distill_min_segment_chars",
                     "distill_max_claims_per_topic", "distill_llm_timeout",
                     "distill_llm_batch"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.http_attempts < 1:
            raise ValueError("http_attempts must be >= 1")
        for name in ("qps", "duration_tolerance", "distill_sentence_pause",
                     "clean_merge_max_gap"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if not 0 <= self.dedupe_threshold <= 64:
            raise ValueError("dedupe_threshold must be between 0 and 64 (dHash bits)")
        if self.distill_backend not in ("auto", "rules", "llm", "agent"):
            raise ValueError(
                f"unknown distill_backend: {self.distill_backend!r} "
                "(auto|rules|llm|agent)")
        return self

    def http_headers(self) -> dict[str, str]:
        h = {"User-Agent": self.user_agent}
        if self.referer:
            h["Referer"] = self.referer
        return h

    def proxies(self) -> dict[str, str] | None:
        """None disables requests' env-proxy lookup; return None when allowed."""
        return None if self.use_system_proxy else {"http": None, "https": None}


def _coerce(raw: str, field: dataclasses.Field) -> Any:
    """Convert an env string using the field's default value as the type probe.

    Fields declared with ``default_factory`` (e.g. ``hotwords: list[str]``) have
    ``MISSING`` as ``default``; probing only that would return the raw string and
    a later loop over hotwords would iterate *characters*. Materialise the
    factory result before deciding.
    """
    default = field.default
    if default is dataclasses.MISSING and field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        default = field.default_factory()  # type: ignore[misc]
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, Path):
        return Path(raw)
    if isinstance(default, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    if default is None and "Path" in str(field.type):
        return Path(raw)
    return raw
