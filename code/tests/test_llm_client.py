"""Offline tests for the model client: provider selection, retries, caching, pricing."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deletion_audit.llm_client import (  # noqa: E402
    DeepSeekBackend, LLMClient, Reply, Usage, deepseek_peak, to_openai_parts)


class FakeBackend:
    provider, model = "fake", "deepseek-flash"

    def __init__(self, replies: list[str]) -> None:
        self.replies, self.calls = list(replies), 0

    def call(self, purpose, system, content, schema) -> Reply:
        self.calls += 1
        return Reply(self.replies.pop(0), Usage("fake", self.model, purpose, 100, 20, 0, 0), True)


def validate(output: dict) -> None:
    if "answer" not in output:
        raise ValueError("missing answer")


def test_corrective_retry_then_cache(tmp_path):
    backend = FakeBackend(["not json", json.dumps({"answer": 42})])
    client = LLMClient(backend, tmp_path)
    args = dict(purpose="t", version="v1", system="s", content=[{"type": "text", "text": "q"}], schema={})
    assert client.complete_json(**args, validate=validate) == {"answer": 42}
    assert backend.calls == 2  # one corrective retry
    assert client.complete_json(**args, validate=validate) == {"answer": 42}
    assert backend.calls == 2  # replayed from the disk cache, no new call
    assert len(client.usage_log.read_text().splitlines()) == 2


def test_schema_failure_twice_returns_none(tmp_path):
    client = LLMClient(FakeBackend(['{"x": 1}', '{"y": 2}']), tmp_path)
    assert client.complete_json(purpose="t", version="v1", system="s", content=[], schema={}, validate=validate) is None


def test_provider_selection_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "")
    assert LLMClient.from_env(tmp_path) is None  # empty key slot -> deterministic only
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    client = LLMClient.from_env(tmp_path)
    assert isinstance(client.backend, DeepSeekBackend) and client.model == "deepseek-flash"


def test_image_blocks_become_data_urls():
    parts = to_openai_parts([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
                             {"type": "text", "text": "read it"}])
    assert parts == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                     {"type": "text", "text": "read it"}]


def test_deepseek_peak_hours_and_offpeak_price():
    assert deepseek_peak(datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc))  # Monday 07:00 UTC
    assert not deepseek_peak(datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc))
    assert not deepseek_peak(datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc))  # Sunday
    peak = Usage("deepseek", "deepseek-flash", "t", 1_000_000, 0, 0, 0, peak=True)
    off = Usage("deepseek", "deepseek-flash", "t", 1_000_000, 0, 0, 0, peak=False)
    assert round(peak.cost_usd, 4) == 0.30 and round(off.cost_usd, 4) == 0.15
