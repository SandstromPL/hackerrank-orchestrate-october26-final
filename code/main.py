"""Entry point: audit every deletion request and write output.csv at the repository root.

    python3 code/main.py                       # full run (518 requests) -> output.csv
    python3 code/main.py --no-llm              # deterministic pipeline only
    python3 code/main.py --requests dataset/sample_deletion_requests.csv --output /tmp/sample.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from deletion_audit.config import DATASET_DIR, OUTPUT_CSV, RUNS_DIR
from deletion_audit.dataset import load_requests
from deletion_audit.output import validate_file, write_output
from deletion_audit.pipeline import AuditPipeline


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ghost Data Still Exists - deletion audit agent")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--requests", type=Path, default=None, help="defaults to <dataset>/deletion_requests.csv")
    parser.add_argument("--output", type=Path, default=OUTPUT_CSV)
    parser.add_argument("--no-llm", action="store_true", help="skip model calls (deterministic baseline)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    requests_path = args.requests or args.dataset / "deletion_requests.csv"
    requests = load_requests(requests_path)
    started_at = datetime.now(timezone.utc)
    pipeline = AuditPipeline(args.dataset, use_llm=not args.no_llm)
    results = pipeline.run(requests)
    write_output(results, args.output)
    problems = validate_file(args.output, [r.case_id for r in requests])
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
    logging.info("wrote %d rows to %s %s", len(results), args.output, counts)
    if pipeline.client is not None:
        fallbacks = [r.case_id for r in results if any(n.startswith("model review unavailable") for n in r.notes)]
        errors = [r.case_id for r in results if any(n.startswith("error:") for n in r.notes)]
        record = RUNS_DIR / f"{started_at:%Y%m%dT%H%M%SZ}.json"
        pipeline.client.save_run(record, started_at=started_at.isoformat(timespec="seconds"),
                                 finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 requests=requests_path.name, output=args.output.name, cases=len(results),
                                 statuses=counts, model_fallback_cases=fallbacks, error_cases=errors)
        logging.info("model answers %s; %d cases kept the deterministic result; run record %s",
                     pipeline.client.call_counts(), len(fallbacks), record.name)
    if pipeline.store.errors:
        logging.warning("evidence files that failed to parse: %s", pipeline.store.errors)
    for problem in problems[:20]:
        logging.error("contract violation: %s", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
