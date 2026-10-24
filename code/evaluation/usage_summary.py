"""Build code/evaluation/usage_report.md for the final full run that produced output.csv.

    python3 code/evaluation/usage_summary.py            # print the report
    python3 code/evaluation/usage_summary.py --write    # write code/evaluation/usage_report.md

`code/main.py` saves a run record listing every model answer the run used: made live, replayed
from the response cache, or failed. Joining it with .cache/llm/usage.jsonl (one line per billed
API call, keyed by the same input hash) finds the original call behind every answer, so the
report covers the full cost of producing output.csv even when some answers came from the cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deletion_audit.config import CACHE_DIR, RUNS_DIR  # noqa: E402
from deletion_audit.llm_client import PRICING  # noqa: E402

REPORT = Path(__file__).resolve().parent / "usage_report.md"
TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
SOURCE_LABELS = {"live": "made during this run", "cache": "replayed from the response cache (billed earlier)",
                 "failed": "failed (no usable answer)"}


def latest_full_run(runs_dir: Path) -> Path:
    for path in sorted(runs_dir.glob("*.json"), reverse=True):
        if json.loads(path.read_text(encoding="utf-8")).get("requests") == "deletion_requests.csv":
            return path
    raise SystemExit(f"no full-run record in {runs_dir}: run python3 code/main.py first")


def billed_rows(run: dict, usage: list[dict]) -> list[tuple[str, dict]]:
    """(source, usage row) for every billed API call behind an answer the run used.

    Answers made or attempted during the run count the calls logged since it started; answers
    replayed from the cache count the earlier calls that produced them.
    """
    source_by_key: dict[str, str] = {}
    for call in run["calls"]:
        source_by_key.setdefault(call["cache_key"], call["source"])
    selected = []
    for row in usage:
        source = source_by_key.get(row["cache_key"])
        if source == "cache" or (source is not None and row.get("at", "") >= run["started_at"]):
            selected.append((source, row))
    return selected


def totals(rows: list[dict]) -> dict[str, float]:
    bucket: dict[str, float] = defaultdict(float)
    for row in rows:
        bucket["calls"] += 1
        bucket["offpeak"] += not row.get("peak", True)
        for name in (*TOKEN_FIELDS, "cost_usd", "seconds"):
            bucket[name] += row.get(name, 0)
    bucket["tokens"] = sum(bucket[name] for name in TOKEN_FIELDS)
    return bucket


def table(groups: list[tuple[str, list[dict]]], cases: int) -> list[str]:
    lines = ["| Scope | API calls | Input (cache miss) | Input (provider cache hit) | Input (cache write) | Output "
             "| Total tokens | Tokens / request | Cost USD | Cost / request USD |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, rows in groups:
        t = totals(rows)
        lines.append(f"| {name} | {t['calls']:.0f} | {t['input_tokens']:,.0f} | {t['cache_read_tokens']:,.0f} | "
                     f"{t['cache_write_tokens']:,.0f} | {t['output_tokens']:,.0f} | {t['tokens']:,.0f} | "
                     f"{t['tokens'] / cases:,.0f} | {t['cost_usd']:.4f} | {t['cost_usd'] / cases:.6f} |")
    return lines


def replay_note(replayed: list[dict]) -> list[str]:
    """When the replayed answers were billed (calls are timestamped since the run-record change)."""
    if not replayed:
        return []
    stamps = sorted(row["at"] for row in replayed if row.get("at"))
    when = f"{len(stamps)} between {stamps[0]} and {stamps[-1]} (UTC)" if stamps else "none timestamped"
    return [f"Replayed answers were billed by earlier runs: {len(replayed)} calls, {when}"
            + (f", {len(replayed) - len(stamps)} before calls were timestamped." if len(stamps) < len(replayed) else "."),
            ""]


def build_report(run: dict, rows: list[tuple[str, dict]], record_name: str) -> str:
    cases = run["cases"]
    answers: dict[str, int] = defaultdict(int)
    for call in run["calls"]:
        answers[call["source"]] += 1
    by_purpose: dict[str, list[dict]] = defaultdict(list)
    by_model: dict[str, list[dict]] = defaultdict(list)
    by_source: dict[str, list[dict]] = defaultdict(list)
    for source, row in rows:
        by_purpose[f"{row['model']} / {row['purpose']}"].append(row)
        by_model[f"{row['provider']} / {row['model']}"].append(row)
        by_source[source].append(row)
    overall = totals([row for _, row in rows])
    live = totals(by_source["live"])
    statuses = ", ".join(f"{name} {count}" for name, count in sorted(run["statuses"].items()))
    fallbacks = run["model_fallback_cases"]
    wall = datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])
    price = PRICING.get(run["model"], {})
    lines = [
        "# Usage report: final full run",
        "",
        f"Generated by `python3 code/evaluation/usage_summary.py --write` from run record `{record_name}`, "
        "which the `python3 code/main.py` run that produced `output.csv` saved.",
        "",
        "## Run",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Started / finished (UTC) | {run['started_at']} / {run['finished_at']} ({wall.total_seconds() / 60:.1f} min) |",
        f"| Requests audited | {cases} (`{run['requests']}`) |",
        f"| Statuses written to `{run['output']}` | {statuses} |",
        f"| Provider / model | {run['provider']} / {run['model']} |",
        f"| Model answers used | {sum(answers.values())}: {answers['live']} made during the run, "
        f"{answers['cache']} replayed from the response cache, {answers['failed']} failed |",
        f"| Cases that kept the deterministic result (no usable model answer) | {len(fallbacks)}"
        + (f": {', '.join(fallbacks)}" if 0 < len(fallbacks) <= 20 else "") + " |",
        f"| Cases that raised an error and got a fallback row | {len(run['error_cases'])} |",
        "",
        "## How the run calls the model",
        "",
        "- One adjudication call per audited case: deletion intent, ownership of softly linked records, and "
        "category refinement. Code merges the answer under fixed guardrails and applies the deletion policy.",
        "- One vision call per screenshot in the evidence manifest, made once at start-up and shared by every case.",
        "- A reply that is not valid JSON or fails schema validation is retried once with the error attached. "
        "Both attempts are billed and counted below.",
        "- Validated answers are cached on disk under a hash of provider, model, prompt version, schema and input. "
        "Replaying an answer costs nothing, so the tables count the original billed call behind every answer the "
        "run used: they are the full cost of producing `output.csv`.",
        "",
        "## Tokens and cost",
        "",
        f"\"Request\" means one of the {cases} deletion requests. \"Provider cache hit\" is the provider's automatic "
        "prompt-prefix cache (the shared system prompt), billed at its cache-hit rate.",
        "",
        "### Per purpose",
        "",
        *table(sorted(by_purpose.items()), cases),
        "",
        "### Per model",
        "",
        *table(sorted(by_model.items()), cases),
        "",
        "### Overall",
        "",
        *table([("overall", [row for _, row in rows])], cases),
        "",
        "### Made during the run vs replayed",
        "",
        *table([(SOURCE_LABELS[s], by_source[s]) for s in ("live", "cache", "failed") if by_source[s]], cases),
        "",
        *replay_note(by_source["cache"]),
        "## Pricing and latency",
        "",
        f"- `{run['model']}` (USD per million tokens): {price.get('input', 0):.3f} input on a cache miss, "
        f"{price.get('cache_read', 0):.3f} on a provider cache hit, {price.get('output', 0):.3f} output."
        + (f" Off-peak calls are billed at {price['offpeak_factor']:.0%} of that "
           "(DeepSeek peak: 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday)."
           if price.get("offpeak_factor", 1.0) < 1.0 else ""),
        f"- {overall['offpeak']:.0f} of {overall['calls']:.0f} billed calls ran off-peak.",
        "- Costs are estimates: the token counts the API returned for each call, priced with the table in "
        "`code/deletion_audit/llm_client.py`.",
        f"- Calls made during the run: {live['calls']:.0f}, {live['seconds'] / 60:.1f} min of model time, "
        f"{live['seconds'] / max(live['calls'], 1):.1f} s per call on average (calls run one at a time).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=None, help="run record (default: latest full run)")
    parser.add_argument("--log", type=Path, default=CACHE_DIR / "llm" / "usage.jsonl")
    parser.add_argument("--write", action="store_true", help=f"write {REPORT.name} instead of printing it")
    args = parser.parse_args(argv)
    record = args.run or latest_full_run(RUNS_DIR)
    run = json.loads(record.read_text(encoding="utf-8"))
    usage = [json.loads(line) for line in args.log.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = build_report(run, billed_rows(run, usage), record.name)
    if args.write:
        REPORT.write_text(report, encoding="utf-8")
        print(f"wrote {REPORT}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
