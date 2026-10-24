"""Per-case orchestration: intent -> identity -> evidence -> policy -> steps.

One failing case never stops the run: it is logged and gets a conservative fallback row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .activity import WorkflowBatches
from .config import CACHE_DIR, DEFAULT_POLICY, AuditPolicy
from .dataset import Dataset, load_requests
from .domain import CaseResult, DeletionRequest, DeletionStatus, Subject, case_sort_key
from .evidence.store import EvidenceStore
from .identity import IdentityIndex
from .intent import IntentResult, classify_intent
from .linking import AccountRecovery, CaseLinker, ResourceView
from .policy import Decision, decide
from .steps import build_steps, status_for

log = logging.getLogger(__name__)


@dataclass
class CaseContext:
    """Everything gathered for one case; handed to the adjudicator and kept for evaluation."""

    request: DeletionRequest
    intent: IntentResult
    subject: Subject | None = None
    views: list[ResourceView] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)


class Adjudicator(Protocol):
    def classify_intent(self, request: DeletionRequest) -> IntentResult | None: ...

    def review(self, context: CaseContext) -> list[Decision] | None: ...


class AuditPipeline:
    def __init__(self, dataset_root: Path, policy: AuditPolicy = DEFAULT_POLICY,
                 adjudicator: Adjudicator | None = None, vision: dict[str, list[dict]] | None = None,
                 cache_dir: Path = CACHE_DIR) -> None:
        self.dataset = Dataset.load(dataset_root)
        self.policy = policy
        self.adjudicator = adjudicator
        self.identities = IdentityIndex(self.dataset.identities, self.dataset.audit_log)
        self.store = EvidenceStore(self.dataset, cache_dir, vision)
        self.batches = WorkflowBatches(self.dataset.activity)
        self.linker = CaseLinker(self.store, self.identities, self.batches, policy)
        # Batch membership needs every known request (inputs only), not just the ones being audited.
        self._all_requests = [r for name in ("deletion_requests.csv", "sample_deletion_requests.csv")
                              if (dataset_root / name).exists() for r in load_requests(dataset_root / name)]
        self._recovered: dict[str, str] | None = None

    def _intent(self, request: DeletionRequest) -> IntentResult:
        if self.adjudicator:
            verdict = self.adjudicator.classify_intent(request)
            if verdict is not None:
                return verdict
        return classify_intent(request.request_text)

    def _recovered_accounts(self) -> dict[str, str]:
        if self._recovered is None:
            self._recovered = {}
            if self.policy.recover_accounts_from_activity:
                pairs = []
                for request in self._all_requests:
                    subject = self.identities.resolve(request.email)
                    if subject and classify_intent(request.request_text).is_deletion:
                        pairs.append((request, subject))
                self._recovered = AccountRecovery(self.linker, self.batches,
                                                  self.policy.match_workflow_outcome).assign(pairs)
        return self._recovered

    def context(self, request: DeletionRequest) -> CaseContext:
        ctx = CaseContext(request=request, intent=self._intent(request))
        if not ctx.intent.is_deletion:
            return ctx
        ctx.subject = self.identities.resolve(request.email)
        if ctx.subject is None:
            return ctx
        ctx.views = self.linker.resources(request, ctx.subject, self._recovered_accounts())
        ctx.decisions = [d for d in (decide(v, self.dataset.rules) for v in ctx.views) if d is not None]
        return ctx

    def audit(self, request: DeletionRequest) -> tuple[CaseResult, CaseContext]:
        ctx = self.context(request)
        if not ctx.intent.is_deletion:
            return CaseResult(request.case_id, DeletionStatus.NOT_APPLICABLE, (), (ctx.intent.basis,)), ctx
        if ctx.subject is None:
            return CaseResult(request.case_id, DeletionStatus.INCOMPLETE, (), ("subject not resolved",)), ctx
        decisions = ctx.decisions
        if self.adjudicator:
            reviewed = self.adjudicator.review(ctx)
            if reviewed is not None:
                decisions = reviewed
        status = status_for(decisions)
        steps = build_steps(decisions, status, verify_compliant=self.policy.verify_compliant_records_when_incomplete)
        return CaseResult(request.case_id, status, tuple(steps), (ctx.intent.basis,)), ctx

    def run(self, requests: list[DeletionRequest]) -> list[CaseResult]:
        results = []
        for request in sorted(requests, key=lambda r: case_sort_key(r.case_id)):
            try:
                result, _ = self.audit(request)
            except Exception as exc:  # noqa: BLE001 - isolation boundary: emit a fallback row and continue
                log.exception("case %s failed", request.case_id)
                result = CaseResult(request.case_id, DeletionStatus.INCOMPLETE, (), (f"error: {exc}",))
            results.append(result)
        return results
