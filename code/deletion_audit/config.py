"""Paths and policy switches. Everything resolves relative to the repository root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "dataset"
OUTPUT_CSV = REPO_ROOT / "output.csv"
CACHE_DIR = REPO_ROOT / ".cache"
RUNS_DIR = CACHE_DIR / "llm" / "runs"  # one record per main.py run: the model answers it used


@dataclass(frozen=True)
class AuditPolicy:
    """Interpretation choices the samples do not fully pin down (see CLAUDE.md §5 "(open)")."""

    # Evidence observed before the request describes the pre-deletion state, so it may link a
    # resource to the subject but cannot establish the state at audit time.
    state_window_starts_at_request: bool = True
    # Verify already-compliant non-account records in INCOMPLETE cases (by analogy with the
    # account/audit checks that the samples always include).
    verify_compliant_records_when_incomplete: bool = True
    # Use deletion-worker activity batches to recover account IDs that no evidence links.
    recover_accounts_from_activity: bool = True
    # When pairing batch rows with requests, require the account step's reported result to agree
    # with the request's reported_status (COMPLETE<->SUCCESS, NOT_STARTED<->FAILED, ...).
    match_workflow_outcome: bool = True


DEFAULT_POLICY = AuditPolicy()
