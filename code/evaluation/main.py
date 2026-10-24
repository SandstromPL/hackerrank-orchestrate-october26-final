"""Score the pipeline against the 32 public samples and our own custom cases.

    python3 code/evaluation/main.py              # run the pipeline on the samples, then score
    python3 code/evaluation/main.py --no-llm     # deterministic baseline only
    python3 code/evaluation/main.py --predictions some.csv   # score an existing file

Each run is saved under .cache/eval/ and compared with the previous run, so every change is
reported as a before -> after delta.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deletion_audit.config import CACHE_DIR, DATASET_DIR, DEFAULT_POLICY  # noqa: E402
from deletion_audit.dataset import load_requests  # noqa: E402
from deletion_audit.output import validate_row  # noqa: E402
from deletion_audit.pipeline import AuditPipeline  # noqa: E402
from deletion_audit.steps import serialize  # noqa: E402
from evaluation.custom_cases import CUSTOM_CASES  # noqa: E402
from evaluation.scoring import Report, score_case  # noqa: E402

RUNS_DIR = CACHE_DIR / "eval"


def read_rows(path: Path) -> dict[str, tuple[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {r["case_id"]: (r["deletion_status"], r["steps_json"]) for r in csv.DictReader(handle)}


def run_pipeline(pipeline: AuditPipeline, requests) -> dict[str, tuple[str, str]]:
    return {r.case_id: (r.status.value, serialize(list(r.steps))) for r in pipeline.run(requests)}


def print_report(title: str, report: Report, verbose: bool) -> dict[str, float]:
    metrics = report.metrics()
    print(f"\n=== {title} ===")
    for name, value in metrics.items():
        print(f"  {name:30} {value:.3f}" if isinstance(value, float) else f"  {name:30} {value}")
    print("  error buckets:")
    for bucket, count in report.buckets().most_common(15):
        print(f"    {count:3}  {bucket}")
    if verbose:
        for case in report.cases:
            if case.buckets:
                print(f"  {case.case_id}: {', '.join(case.buckets)}")
    return metrics


def compare(previous: dict, current: dict) -> None:
    print("\n=== delta vs previous run ===")
    for name, value in current.items():
        old = previous.get(name)
        if isinstance(value, float) and isinstance(old, float) and abs(value - old) > 1e-9:
            print(f"  {name:30} {old:.3f} -> {value:.3f} ({value - old:+.3f})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, help="score this CSV instead of running the pipeline")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="hide per-case buckets")
    parser.add_argument("--label", default="", help="name for this run in .cache/eval/")
    parser.add_argument("--set", action="append", default=[], metavar="FLAG=true|false",
                        help="override an AuditPolicy flag for an ablation run")
    args = parser.parse_args(argv)
    overrides = {k: v.lower() == "true" for k, v in (item.split("=", 1) for item in args.set)}
    policy = replace(DEFAULT_POLICY, **overrides)

    expected = read_rows(DATASET_DIR / "sample_output.csv")
    pipeline = None
    if args.predictions:
        predicted = read_rows(args.predictions)
    else:
        pipeline = AuditPipeline(DATASET_DIR, policy)
        predicted = run_pipeline(pipeline, load_requests(DATASET_DIR / "sample_deletion_requests.csv"))
    invalid = {cid: validate_row(*row) for cid, row in predicted.items() if validate_row(*row)}
    sample_report = Report([score_case(cid, expected[cid], predicted.get(cid, ("MISSING", "[]")))
                            for cid in sorted(expected, key=lambda c: int(c.split("_")[1]))])
    metrics = {"samples": print_report("32 public samples", sample_report, not args.quiet)}
    if pipeline is not None:
        custom_predicted = run_pipeline(pipeline, [c.request for c in CUSTOM_CASES])
        custom_report = Report([score_case(c.request.case_id, (c.expected_status, json.dumps(c.expected_steps)),
                                           custom_predicted[c.request.case_id]) for c in CUSTOM_CASES])
        metrics["custom"] = print_report(f"{len(CUSTOM_CASES)} custom cases", custom_report, True)
    print(f"\n  contract violations: {len(invalid)}" + (f" {invalid}" if invalid else ""))

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    latest = RUNS_DIR / "latest.json"
    if latest.exists():
        previous = json.loads(latest.read_text())
        compare(previous.get("metrics", {}).get("samples", {}), metrics["samples"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record = {"label": args.label, "at": stamp, "policy": overrides, "metrics": metrics}
    (RUNS_DIR / f"{stamp}{'-' + args.label if args.label else ''}.json").write_text(json.dumps(record, indent=2))
    latest.write_text(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
