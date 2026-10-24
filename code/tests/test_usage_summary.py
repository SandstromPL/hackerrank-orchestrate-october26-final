"""The usage report counts the billed call behind every answer a run used, and nothing else."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.usage_summary import billed_rows  # noqa: E402


def test_billed_rows_join_run_record_with_usage_log():
    run = {"started_at": "2026-10-24T16:00:00+00:00",
           "calls": [{"purpose": "screenshot", "cache_key": "aaa", "source": "cache"},
                     {"purpose": "adjudication", "cache_key": "bbb", "source": "live"},
                     {"purpose": "adjudication", "cache_key": "ccc", "source": "failed"}]}
    usage = [
        {"cache_key": "aaa", "at": "2026-10-24T12:00:00+00:00"},  # billed earlier, replayed by this run
        {"cache_key": "bbb"},  # an older failed attempt at the same input: not this run's cost
        {"cache_key": "bbb", "at": "2026-10-24T16:05:00+00:00"},  # answered during the run
        {"cache_key": "ccc", "at": "2026-10-24T16:06:00+00:00"},  # unusable attempt during the run
        {"cache_key": "zzz", "at": "2026-10-24T16:07:00+00:00"},  # some other run's call
    ]
    assert [(source, row["cache_key"]) for source, row in billed_rows(run, usage)] == [
        ("cache", "aaa"), ("live", "bbb"), ("failed", "ccc")]
