"""Write and validate output.csv (`case_id,deletion_status,steps_json`); write review_queue.csv."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .domain import (OPERATION_PHASE, OPERATION_STATE, PHASE_ORDER, CaseResult, DeletionStatus, Operation, Phase,
                     ReviewReason, case_sort_key)
from .steps import serialize

HEADER = ["case_id", "deletion_status", "steps_json"]
REVIEW_HEADER = ["case_id", "deletion_status", "priority", "reason", "detail"]
REVIEW_ORDER = {reason: rank for rank, reason in enumerate(ReviewReason)}  # declaration order: most severe first
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def write_output(results: list[CaseResult], path: Path) -> None:
    rows = sorted(results, key=lambda r: case_sort_key(r.case_id))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for result in rows:
            writer.writerow([result.case_id, result.status.value, serialize(list(result.steps))])


def spreadsheet_safe(value: object) -> str:
    """People open the review queue in spreadsheets, and IDs come from untrusted evidence, so a cell
    that would start a formula is escaped with a leading apostrophe."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def write_review_queue(results: list[CaseResult], path: Path) -> int:
    """One row per (case, reason) a person should check; returns the number of rows written."""
    rows = sorted(((r.case_id, r.status.value, item.reason.priority, item.reason.value, item.detail)
                   for r in results for item in set(r.review)),
                  key=lambda row: (case_sort_key(row[0]), REVIEW_ORDER[ReviewReason(row[3])], row[4]))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(REVIEW_HEADER)
        writer.writerows([spreadsheet_safe(cell) for cell in row] for row in rows)
    return len(rows)


def validate_row(status: str, steps_json: str) -> list[str]:
    """Contract violations for one row (empty list when valid)."""
    problems: list[str] = []
    try:
        steps = json.loads(steps_json)
    except ValueError:
        return ["steps_json is not valid JSON"]
    if not isinstance(steps, list):
        return ["steps_json is not an array"]
    if status not in {s.value for s in DeletionStatus}:
        problems.append(f"bad status {status}")
    if status == DeletionStatus.NOT_APPLICABLE.value and steps:
        problems.append("NOT_APPLICABLE must have []")
    last_phase = -1
    for number, step in enumerate(steps, start=1):
        if set(step) != {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}:
            problems.append(f"step {number}: wrong keys")
            continue
        if step["step_no"] != number:
            problems.append(f"step {number}: step_no {step['step_no']}")
        try:
            operation, phase = Operation(step["operation"]), Phase(step["phase"])
        except ValueError:
            problems.append(f"step {number}: unknown operation/phase")
            continue
        if OPERATION_PHASE[operation] != phase or OPERATION_STATE[operation] != step["state"]:
            problems.append(f"step {number}: operation/phase/state mismatch")
        if PHASE_ORDER[phase] < last_phase:
            problems.append(f"step {number}: phase order")
        last_phase = PHASE_ORDER[phase]
        cites = bool(step["evidence_id"])
        if phase == Phase.FINDING and not cites:
            problems.append(f"step {number}: finding without evidence")
        if phase == Phase.REMEDIATION and cites:
            problems.append(f"step {number}: remediation cites evidence")
        if status == DeletionStatus.COMPLETE.value and (phase != Phase.VERIFICATION or not cites):
            problems.append(f"step {number}: COMPLETE needs evidence-backed verification only")
        if status == DeletionStatus.INCOMPLETE.value and phase == Phase.VERIFICATION and cites:
            problems.append(f"step {number}: future verification cites evidence")
    return problems


def validate_file(path: Path, expected_case_ids: list[str]) -> list[str]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = list(reader)
    problems = [] if header == HEADER else [f"header {header}"]
    ids = [row[0] for row in rows]
    if ids != sorted(ids, key=case_sort_key):
        problems.append("rows not sorted by case_id")
    if sorted(ids) != sorted(expected_case_ids):
        problems.append(f"case ids differ from requests ({len(ids)} rows vs {len(expected_case_ids)} requests)")
    for row in rows:
        problems += [f"{row[0]}: {p}" for p in validate_row(row[1], row[2])]
    return problems
