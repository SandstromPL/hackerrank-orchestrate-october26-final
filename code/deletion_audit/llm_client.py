"""Model client: one interface (`complete_json`) over two providers (DeepSeek, Anthropic).

Determinism does not come from sampling settings: every validated response is cached on disk
under a hash of (provider, model, prompt version, schema, payload), so a re-run with the same
inputs replays the same answers at zero cost. A reply that is not valid JSON or fails the
caller's schema check is retried once with the error attached; any other failure returns None
and the caller falls back to the deterministic pipeline.

`complete_many` runs independent calls on a thread pool (`LLM_MAX_CONCURRENCY` in flight, call
starts spaced to `LLM_CALLS_PER_SECOND`); the SDKs back off and retry on 429s. Answers are also
memoized for the run, so asking again for the same input never calls (or bills) twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .config import REPO_ROOT

log = logging.getLogger(__name__)

# USD per million tokens. DeepSeek bills half price off-peak (pricing page, Oct 2026).
PRICING: dict[str, dict[str, float]] = {
    "deepseek-flash": {"input": 0.30, "output": 1.20, "cache_read": 0.006, "offpeak_factor": 0.5},
    "deepseek-v4-pro": {"input": 1.32, "output": 3.96, "cache_read": 0.044, "offpeak_factor": 0.5},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "offpeak_factor": 1.0},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "offpeak_factor": 1.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "offpeak_factor": 1.0},
}
JSON_INSTRUCTION = ("\n\nRespond with a single json object (no prose, no code fences) that validates against "
                    "this JSON Schema:\n")
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_CALLS_PER_SECOND = 4.0


class ProviderError(RuntimeError):
    """The provider could not produce an answer (network, auth, quota, refusal)."""


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


def env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("%s is not a number: using %s", name, default)
        return default


class RateLimiter:
    """Spaces call starts at least 1/rate seconds apart across threads (rate <= 0: no limit)."""

    def __init__(self, calls_per_second: float) -> None:
        self._interval = 1.0 / calls_per_second if calls_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + self._interval
        if start > now:
            time.sleep(start - now)


def deepseek_peak(at: datetime) -> bool:
    """DeepSeek peak hours: 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday."""
    at = at.astimezone(timezone.utc)
    return at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)


@dataclass(frozen=True)
class Usage:
    provider: str
    model: str
    purpose: str
    input_tokens: int  # billed at the full input rate (cache misses)
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    peak: bool = True

    @property
    def cost_usd(self) -> float:
        price = PRICING.get(self.model, PRICING["claude-opus-5-5"])
        factor = 1.0 if self.peak else price["offpeak_factor"]
        return factor * (self.input_tokens * price["input"] + self.output_tokens * price["output"]
                         + self.cache_read_tokens * price["cache_read"]
                         + self.cache_write_tokens * price["input"] * 1.25) / 1_000_000


@dataclass(frozen=True)
class CallRecord:
    """One answer the pipeline asked for, and where it came from."""

    purpose: str
    cache_key: str  # first 24 hex chars of the input hash, as in the cache file name and usage.jsonl
    source: str  # "live" (answered now), "cache" (replayed from disk), "failed" (no usable answer)


@dataclass(frozen=True)
class Reply:
    text: str
    usage: Usage
    complete: bool  # False when the model stopped early (length, refusal, filter)


class Backend(Protocol):
    provider: str
    model: str

    def call(self, purpose: str, system: str, content: list[dict], schema: dict) -> Reply: ...


def to_openai_parts(content: list[dict]) -> list[dict]:
    """Neutral content blocks (Anthropic shape) -> OpenAI chat parts used by DeepSeek."""
    parts = []
    for block in content:
        if block.get("type") == "image":
            source = block["source"]
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"}})
        else:
            parts.append({"type": "text", "text": block.get("text", "")})
    return parts


class DeepSeekBackend:
    provider = "deepseek"

    def __init__(self, api_key: str, model: str, base_url: str, max_tokens: int = 8000) -> None:
        import openai  # lazy: the deterministic path needs no SDK

        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=3, timeout=180.0)
        self.model = model
        self._max_tokens = max_tokens

    def call(self, purpose: str, system: str, content: list[dict], schema: dict) -> Reply:
        messages = [
            {"role": "system", "content": system + JSON_INSTRUCTION + json.dumps(schema, sort_keys=True)},
            {"role": "user", "content": to_openai_parts(content)},
        ]
        try:
            response = self._client.chat.completions.create(
                model=self.model, messages=messages, response_format={"type": "json_object"},
                max_tokens=self._max_tokens)
        except self._openai.APIError as exc:
            raise ProviderError(str(exc)) from exc
        choice = response.choices[0]
        usage = response.usage
        extra = getattr(usage, "model_extra", None) or {}
        hit = int(getattr(usage, "prompt_cache_hit_tokens", None) or extra.get("prompt_cache_hit_tokens") or 0)
        prompt = int(usage.prompt_tokens or 0) if usage else 0
        return Reply(
            text=choice.message.content or "",
            usage=Usage(self.provider, self.model, purpose, max(prompt - hit, 0),
                        int(usage.completion_tokens or 0) if usage else 0, hit, 0,
                        deepseek_peak(datetime.now(timezone.utc))),
            complete=choice.finish_reason == "stop",
        )


class AnthropicBackend:
    provider = "anthropic"

    def __init__(self, model: str, effort: str, max_tokens: int = 8000) -> None:
        import anthropic  # lazy: the deterministic path needs no SDK

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(max_retries=3, timeout=180.0)
        self.model = model
        self._effort = effort
        self._max_tokens = max_tokens

    def call(self, purpose: str, system: str, content: list[dict], schema: dict) -> Reply:
        try:
            response = self._client.messages.create(
                model=self.model, max_tokens=self._max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except self._anthropic.APIError as exc:
            raise ProviderError(str(exc)) from exc
        u = response.usage
        return Reply(
            text=next((b.text for b in response.content if b.type == "text"), ""),
            usage=Usage(self.provider, self.model, purpose, u.input_tokens, u.output_tokens,
                        u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0),
            complete=response.stop_reason == "end_turn",
        )


class LLMClient:
    def __init__(self, backend: Backend, cache_dir: Path, *, max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
                 calls_per_second: float = DEFAULT_CALLS_PER_SECOND) -> None:
        self.backend = backend
        self._cache_dir = cache_dir / "llm"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage_log = self._cache_dir / "usage.jsonl"
        self.calls: list[CallRecord] = []
        self.max_concurrency = max(1, max_concurrency)
        self.calls_per_second = calls_per_second
        self._limiter = RateLimiter(calls_per_second)
        self._answers: dict[str, dict | None] = {}  # input hash -> answer already obtained this run
        self._lock = threading.Lock()  # guards calls, the key locks and the usage log
        self._key_locks: dict[str, threading.Lock] = {}

    @property
    def model(self) -> str:
        return self.backend.model

    @classmethod
    def from_env(cls, cache_dir: Path) -> "LLMClient | None":
        """Pick the provider from .env / environment; None means deterministic-only."""
        load_env_file()
        provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
        if not provider:
            provider = "deepseek" if os.environ.get("DEEPSEEK_API_KEY") else (
                "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "")
        try:
            if provider == "deepseek" and os.environ.get("DEEPSEEK_API_KEY"):
                backend: Backend = DeepSeekBackend(os.environ["DEEPSEEK_API_KEY"],
                                                   os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                                                   os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
            elif provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
                backend = AnthropicBackend(os.environ.get("AUDIT_MODEL", "claude-opus-5-5"),
                                           os.environ.get("AUDIT_EFFORT", "medium"))
            else:
                log.info("no model API key configured: running the deterministic pipeline only")
                return None
        except ImportError as exc:
            log.warning("model SDK missing (%s): running the deterministic pipeline only", exc)
            return None
        client = cls(backend, cache_dir,
                     max_concurrency=int(env_number("LLM_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)),
                     calls_per_second=env_number("LLM_CALLS_PER_SECOND", DEFAULT_CALLS_PER_SECOND))
        log.info("model review enabled: %s / %s (up to %d calls in flight, %.1f starts/s)", backend.provider,
                 backend.model, client.max_concurrency, client.calls_per_second)
        return client

    def _key(self, purpose: str, version: str, system: str, content: object, schema: dict) -> str:
        blob = json.dumps({"provider": self.backend.provider, "model": self.backend.model, "purpose": purpose,
                           "version": version, "system": system, "content": content, "schema": schema},
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _log_usage(self, reply: Reply, key: str, seconds: float, attempt: int) -> None:
        record = {**asdict(reply.usage), "cost_usd": round(reply.usage.cost_usd, 6), "cache_key": key[:24],
                  "complete": reply.complete, "attempt": attempt, "seconds": round(seconds, 2),
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with self._lock, self.usage_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def _record(self, purpose: str, key: str, source: str) -> None:
        with self._lock:
            self.calls.append(CallRecord(purpose, key[:24], source))

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def complete_json(self, *, purpose: str, version: str, system: str, content: list[dict], schema: dict,
                      validate: Callable[[dict], object] | None = None) -> dict | None:
        """One structured call (plus one corrective retry). Returns the parsed JSON or None.

        Thread-safe. The same input asked twice in one run (e.g. by the parallel prefetch and then
        the per-case pass) is answered once; a concurrent duplicate waits for the first.
        """
        key = self._key(purpose, version, system, content, schema)
        with self._key_lock(key):
            if key not in self._answers:
                self._answers[key] = self._answer(purpose, key, system, content, schema, validate)
            return self._answers[key]

    def complete_many(self, jobs: list[dict[str, Any]]) -> list[dict | None]:
        """complete_json(**job) for every job, up to max_concurrency at once; results keep the jobs' order.

        A job that raises yields None here; the caller's own per-case handling meets the error again.
        """
        def run(job: dict[str, Any]) -> dict | None:
            try:
                return self.complete_json(**job)
            except Exception:  # noqa: BLE001 - one bad job must not sink the batch
                log.exception("%s call raised", job.get("purpose"))
                return None

        if self.max_concurrency == 1 or len(jobs) < 2:
            return [run(job) for job in jobs]
        with ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="llm") as pool:
            return list(pool.map(run, jobs))

    def _answer(self, purpose: str, key: str, system: str, content: list[dict], schema: dict,
                validate: Callable[[dict], object] | None) -> dict | None:
        cached = self._cache_dir / f"{purpose}-{key[:24]}.json"
        if cached.exists():
            self._record(purpose, key, "cache")
            return json.loads(cached.read_text(encoding="utf-8"))["output"]
        attempt_content = list(content)
        for attempt in (1, 2):
            self._limiter.wait()
            started = time.monotonic()
            try:
                reply = self.backend.call(purpose, system, attempt_content, schema)
            except ProviderError as exc:
                log.warning("%s call failed: %s", purpose, exc)
                self._record(purpose, key, "failed")
                return None
            self._log_usage(reply, key, time.monotonic() - started, attempt)
            problem = None if reply.complete else "the reply was cut off"
            output: dict | None = None
            if problem is None:
                try:
                    output = json.loads(reply.text)
                    if not isinstance(output, dict):
                        raise ValueError("top-level value is not an object")
                    if validate:
                        validate(output)
                except ValueError as exc:  # json.JSONDecodeError and pydantic.ValidationError are ValueErrors
                    problem = f"the reply did not match the schema: {str(exc)[:500]}"
            if problem is None and output is not None:
                # Write-then-rename, so a crash or a concurrent reader never sees half a cache file.
                partial = cached.with_name(f"{cached.name}.{threading.get_ident()}.tmp")
                partial.write_text(json.dumps({"purpose": purpose, "model": self.backend.model, "output": output},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(partial, cached)
                self._record(purpose, key, "live")
                return output
            log.warning("%s attempt %d unusable: %s", purpose, attempt, problem)
            attempt_content = list(content) + [{"type": "text", "text": f"Your previous reply was unusable: {problem}. "
                                                "Return only the json object required by the schema."}]
        self._record(purpose, key, "failed")
        return None

    def call_counts(self) -> dict[str, int]:
        counts = {"live": 0, "cache": 0, "failed": 0}
        with self._lock:
            for call in self.calls:
                counts[call.source] += 1
        return counts

    def save_run(self, path: Path, **meta: object) -> None:
        """Write the run record that usage_summary.py joins with usage.jsonl for usage_report.md."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:  # sorted: parallel calls finish in any order
            calls = [asdict(call) for call in sorted(self.calls, key=lambda c: (c.purpose, c.cache_key, c.source))]
        record = {"provider": self.backend.provider, "model": self.backend.model,
                  "max_concurrency": self.max_concurrency, "calls_per_second": self.calls_per_second, **meta,
                  "calls": calls}
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
