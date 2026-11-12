"""Model access: credential resolution plus one request interface.

Two commercial shapes, one interface, so the engine never learns which is behind
it:

* ``byok`` — the user brings a base URL and an API key (available now);
* ``managed`` — the vendor runs a gateway that owns identity, quota and billing
  (the hook for the official version; same wire format, different auth source).

Keeping this boundary explicit is what makes switching from BYOK to a paid
gateway a configuration change instead of a rewrite.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests
import yaml

CREDENTIALS_PATH = Path.home() / ".config" / "v2n" / "credentials.yaml"


@dataclass(frozen=True)
class ModelAccess:
    """Everything needed to call one model, plus what it costs."""

    base_url: str = ""
    model: str = ""
    api_key: str = ""
    price_in: float = 0.0     # currency units per 1M input tokens
    price_out: float = 0.0
    kind: str = "byok"

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())

    def cost(self, tokens_in: int, tokens_out: int) -> float | None:
        """Estimated cost, or None when no price is configured.

        Returning None rather than 0 matters: "free" and "unknown" are different
        answers, and a paid product must not silently report a wrong number.
        """
        if self.price_in <= 0 and self.price_out <= 0:
            return None
        return (tokens_in / 1_000_000) * self.price_in \
            + (tokens_out / 1_000_000) * self.price_out


class Provider(Protocol):
    access: ModelAccess

    def chat(self, messages: list[dict[str, str]], *, timeout: float,
             ) -> tuple[str, dict[str, int]]: ...


class OpenAICompatible:
    """Minimal ``/chat/completions`` client. No SDK, no vendor lock."""

    def __init__(self, access: ModelAccess):
        self.access = access
        self.url = access.base_url.rstrip("/") + "/chat/completions"

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.access.api_key:
            headers["Authorization"] = f"Bearer {self.access.api_key}"
        return headers

    def payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {"model": self.access.model, "temperature": 0,
                "messages": messages}

    def chat(self, messages: list[dict[str, str]], *, timeout: float,
             ) -> tuple[str, dict[str, int]]:
        try:
            response = requests.post(self.url, headers=self.headers(),
                                     json=self.payload(messages), timeout=timeout)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            # Never surface the request body: it carries the transcript.
            raise RuntimeError(
                f"provider {self.access.model!r} failed: {type(exc).__name__}: {exc}"
            ) from exc
        usage = body.get("usage") or {}
        return content, {
            "tokens_in": int(usage.get("prompt_tokens") or 0),
            "tokens_out": int(usage.get("completion_tokens") or 0),
        }


class ManagedGateway(OpenAICompatible):
    """Placeholder for the paid gateway.

    Same wire format as BYOK, but the key is a session token issued by the
    vendor's account system and the base URL points at the vendor. Quota
    errors (402/429) are surfaced verbatim so the UI can prompt a top-up.
    """

    def __init__(self, access: ModelAccess):
        if not access.api_key:
            raise RuntimeError(
                "managed gateway requires a session token; "
                "sign in through the app or configure one explicitly")
        super().__init__(access)


def load_credentials(path: Path | None = None) -> dict[str, Any]:
    """Read the local credential file; a missing file is not an error."""
    target = Path(path) if path else CREDENTIALS_PATH
    if not target.exists():
        return {}
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def save_credentials(data: dict[str, Any], path: Path | None = None) -> Path:
    """Write the credential file with owner-only permissions where supported."""
    target = Path(path) if path else CREDENTIALS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True),
                      encoding="utf-8")
    with contextlib.suppress(OSError):
        target.chmod(0o600)  # Windows may ignore POSIX modes; documented, not fatal
    return target


def resolve_access(cfg: Any, *, credentials: dict[str, Any] | None = None,
                   ) -> ModelAccess:
    """Merge config, credential file and environment into one access record.

    Precedence: explicit config > credential file > environment variable. The
    env-var fallback exists so CI can inject a key without writing files.
    """
    stored = load_credentials() if credentials is None else credentials
    base_url = str(getattr(cfg, "distill_llm_base_url", "") or "").strip() \
        or str(stored.get("base_url", "") or "").strip()
    model = str(getattr(cfg, "distill_llm_model", "") or "").strip() \
        or str(stored.get("model", "") or "").strip()
    key_env = str(getattr(cfg, "distill_llm_key_env", "") or "V2N_LLM_API_KEY")
    api_key = str(stored.get("api_key", "") or "").strip() \
        or os.environ.get(key_env, "").strip()
    return ModelAccess(
        base_url=base_url,
        model=model,
        api_key=api_key,
        price_in=float(getattr(cfg, "distill_llm_price_in", 0.0) or 0.0),
        price_out=float(getattr(cfg, "distill_llm_price_out", 0.0) or 0.0),
        kind=str(stored.get("kind", "byok") or "byok"),
    )


def make_provider(access: ModelAccess) -> Provider:
    if access.kind == "managed":
        return ManagedGateway(access)
    return OpenAICompatible(access)
