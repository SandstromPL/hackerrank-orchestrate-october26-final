"""Summarize .cache/llm/usage.jsonl (one line per live model call) for usage_report.md.

    python3 code/evaluation/usage_summary.py --requests 518

Cached replays cost nothing and are not logged, so run this right after the final full run that
produced output.csv (start that run from an empty .cache/llm/ to account for every call).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deletion_audit.config import CACHE_DIR  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, default=CACHE_DIR / "llm" / "usage.jsonl")
    parser.add_argument("--requests", type=int, default=518)
    args = parser.parse_args(argv)
    rows = [json.loads(line) for line in args.log.read_text(encoding="utf-8").splitlines() if line.strip()]
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        for key in (("model", row["model"]), ("purpose", f"{row['model']} / {row['purpose']}"), ("all", "overall")):
            bucket = totals[key]
            bucket["calls"] += 1
            for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd"):
                bucket[name] += row[name]
    print("| Scope | Calls | Input | Cache read | Cache write | Output | Total tokens | Tokens/request | Cost USD | Cost/request |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for (kind, name), b in sorted(totals.items(), key=lambda kv: ({"purpose": 0, "model": 1, "all": 2}[kv[0][0]], kv[0][1])):
        total = b["input_tokens"] + b["output_tokens"] + b["cache_read_tokens"] + b["cache_write_tokens"]
        print(f"| {name} | {int(b['calls'])} | {int(b['input_tokens'])} | {int(b['cache_read_tokens'])} | "
              f"{int(b['cache_write_tokens'])} | {int(b['output_tokens'])} | {int(total)} | {total / args.requests:.0f} | "
              f"{b['cost_usd']:.4f} | {b['cost_usd'] / args.requests:.5f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
