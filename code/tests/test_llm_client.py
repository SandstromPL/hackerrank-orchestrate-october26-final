"""Offline tests for the model client: provider selection, retries, caching, pricing, concurrency."""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deletion_audit.llm_client import (  # noqa: E402
    DeepSeekBackend, LLMClient, RateLimiter, Reply, Usage, deepseek_peak, to_openai_parts)


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
    assert LLMClient(FakeBackend([]), tmp_path).complete_json(**args, validate=validate) == {"answer": 42}
    assert backend.calls == 2  # a later run replays the disk cache: no new call
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


def test_call_records_track_where_answers_came_from(tmp_path):
    ask = dict(purpose="t", version="v1", system="s", schema={}, validate=validate)
    first = LLMClient(FakeBackend([json.dumps({"answer": 1}), "not json", "still not json"]), tmp_path)
    first.complete_json(content=[{"type": "text", "text": "a"}], **ask)  # answered live
    first.complete_json(content=[{"type": "text", "text": "a"}], **ask)  # same run: memoized, not asked or counted again
    first.complete_json(content=[{"type": "text", "text": "b"}], **ask)  # unusable twice
    assert [call.source for call in first.calls] == ["live", "failed"]
    later = LLMClient(FakeBackend([]), tmp_path)
    later.complete_json(content=[{"type": "text", "text": "a"}], **ask)  # a later run replays the disk cache
    assert later.call_counts() == {"live": 0, "cache": 1, "failed": 0}
    assert first.calls[0].cache_key == later.calls[0].cache_key


class SlowBackend:
    provider, model = "fake", "deepseek-flash"

    def __init__(self) -> None:
        self.lock, self.calls, self.in_flight, self.peak = threading.Lock(), 0, 0, 0

    def call(self, purpose, system, content, schema) -> Reply:
        with self.lock:
            self.calls, self.in_flight = self.calls + 1, self.in_flight + 1
            self.peak = max(self.peak, self.in_flight)
        time.sleep(0.05)
        with self.lock:
            self.in_flight -= 1
        return Reply(json.dumps({"answer": content[0]["text"]}), Usage("fake", self.model, purpose, 10, 5, 0, 0), True)


def test_complete_many_is_parallel_ordered_and_asks_each_input_once(tmp_path):
    backend = SlowBackend()
    client = LLMClient(backend, tmp_path, max_concurrency=4, calls_per_second=0)
    texts = [str(n) for n in range(12)] + ["3"]  # "3" is asked twice
    answers = client.complete_many([dict(purpose="t", version="v1", system="s", schema={}, validate=validate,
                                         content=[{"type": "text", "text": t}]) for t in texts])
    assert [a["answer"] for a in answers] == texts  # results keep the jobs' order
    assert backend.calls == 12  # the duplicate waited for the first answer instead of calling again
    assert 1 < backend.peak <= 4  # concurrent, never above the limit
    assert client.call_counts() == {"live": 12, "cache": 0, "failed": 0}


def test_rate_limiter_spaces_call_starts():
    limiter = RateLimiter(20.0)  # one start every 50 ms
    started = time.monotonic()
    for _ in range(5):
        limiter.wait()
    assert time.monotonic() - started >= 0.19  # four 50 ms gaps; the first start is immediate
