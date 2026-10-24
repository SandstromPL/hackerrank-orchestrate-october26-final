"""Thin Claude client: structured JSON output, disk cache, token/cost accounting.

Determinism does not come from sampling settings (the API no longer accepts temperature): every
response is cached on disk under a hash of (model, effort, prompt version, schema, payload), so a
re-run with the same inputs replays the same answers at zero cost. Any failure returns None and
the caller falls back to the deterministic pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_ROOT

log = logging.getLogger(__name__)

# USD per million tokens (Anthropic first-party pricing, Oct 2026). Cache reads bill at the
# cache-read rate; cache writes at 1.25x input.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01},
}


def load_env_file(path: Path = REPO_ROOT / ".env") -> None:
    """Read KEY=VALUE lines from .env into os.environ (without overriding real env vars)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Usage:
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def cost_usd(self) -> float:
        price = PRICING.get(self.model, PRICING["claude-opus-5-5"])
        return (self.input_tokens * price["input"] + self.output_tokens * price["output"]
                + self.cache_read_tokens * price["cache_read"]
                + self.cache_write_tokens * price["input"] * 1.25) / 1_000_000


class LLMClient:
    def __init__(self, model: str, effort: str, cache_dir: Path, max_tokens: int = 8000) -> None:
        import anthropic  # imported lazily so the deterministic path needs no SDK

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(max_retries=3, timeout=120.0)
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._cache_dir = cache_dir / "llm"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage_log = self._cache_dir / "usage.jsonl"

    @classmethod
    def from_env(cls, cache_dir: Path) -> "LLMClient | None":
        load_env_file()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.info("ANTHROPIC_API_KEY not set: running the deterministic pipeline only")
            return None
        try:
            return cls(os.environ.get("AUDIT_MODEL", "claude-opus-5-5"), os.environ.get("AUDIT_EFFORT", "medium"),
                       cache_dir)
        except ImportError:
            log.warning("anthropic SDK not installed: running the deterministic pipeline only")
            return None

    def _key(self, purpose: str, version: str, system: str, content: object, schema: dict) -> str:
        blob = json.dumps({"model": self.model, "effort": self.effort, "purpose": purpose, "version": version,
                           "system": system, "content": content, "schema": schema}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def complete_json(self, *, purpose: str, version: str, system: str, content: list[dict],
                      schema: dict) -> dict | None:
        """One structured-output call. Returns the parsed JSON object, or None on any failure."""
        key = self._key(purpose, version, system, content, schema)
        cached = self._cache_dir / f"{purpose}-{key[:24]}.json"
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))["output"]
        started = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except self._anthropic.APIError as exc:
            log.warning("%s call failed: %s", purpose, exc)
            return None
        usage = Usage(self.model, purpose, response.usage.input_tokens, response.usage.output_tokens,
                      response.usage.cache_read_input_tokens or 0, response.usage.cache_creation_input_tokens or 0)
        with self.usage_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**usage.__dict__, "cost_usd": round(usage.cost_usd, 6), "cache_key": key[:24],
                                     "stop_reason": response.stop_reason,
                                     "seconds": round(time.monotonic() - started, 2)}) + "\n")
        if response.stop_reason != "end_turn":
            log.warning("%s call stopped with %s", purpose, response.stop_reason)
            return None
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            output = json.loads(text)
        except ValueError:
            log.warning("%s call returned invalid JSON", purpose)
            return None
        cached.write_text(json.dumps({"purpose": purpose, "model": self.model, "output": output}, ensure_ascii=False,
                                     indent=1), encoding="utf-8")
        return output
