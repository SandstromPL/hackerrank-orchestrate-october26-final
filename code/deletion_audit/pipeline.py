"""Per-case orchestration: identity -> evidence -> (model review) -> policy -> steps.

The deterministic pass always runs first and is the fallback. When a model is configured it
reviews each case once (intent, ownership of soft links, categories) and code re-applies the
policy to the reviewed view. One failing case never stops the run: it is logged and gets a
conservative fallback row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .activity import WorkflowBatches
from .adjudication import CaseAdjudicator
from .config import CACHE_DIR, DEFAULT_POLICY, AuditPolicy
from .dataset import Dataset, load_requests
from .domain import CaseResult, DeletionRequest, DeletionStatus, Subject, case_sort_key
from .evidence.store import EvidenceStore
from .identity import IdentityIndex
from .intent import IntentResult, classify_intent
from .linking import AccountRecovery, CaseLinker, ResourceView
from .llm_client import LLMClient
from .policy import Decision, decide
from .screenshot_reader import read_screenshots
from .steps import build_steps, status_for

log = logging.getLogger(__name__)


@dataclass
class CaseContext:
    """Everything gathered for one case; kept for evaluation and debugging."""

    request: DeletionRequest
    intent: IntentResult
    subject: Subject | None = None
    views: list[ResourceView] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class AuditPipeline:
    def __init__(self, dataset_root: Path, policy: AuditPolicy = DEFAULT_POLICY, *, use_llm: bool = False,
                 cache_dir: Path = CACHE_DIR) -> None:
        self.dataset = Dataset.load(dataset_root)
        self.policy = policy
        self.client = LLMClient.from_env(cache_dir) if use_llm else None
        vision = read_screenshots(self.client, self.dataset) if self.client else None
        self.adjudicator = CaseAdjudicator(self.client, self.dataset.activity) if self.client else None
        self.identities = IdentityIndex(self.dataset.identities, self.dataset.audit_log)
        self.store = EvidenceStore(self.dataset, cache_dir, vision)
        self.batches = WorkflowBatches(self.dataset.activity)
        self.linker = CaseLinker(self.store, self.identities, self.batches, policy)
        # Batch membership needs every known request (inputs only), not just the ones being audited.
        self._all_requests = [r for name in ("deletion_requests.csv", "sample_deletion_requests.csv")
                              if (dataset_root / name).exists() for r in load_requests(dataset_root / name)]
        self._recovered: dict[str, str] | None = None

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

    def _decide(self, views: list[ResourceView]) -> list[Decision]:
        return [d for d in (decide(v, self.dataset.rules) for v in views) if d is not None]

    def _excerpts(self, views: list[ResourceView]) -> dict[str, list[str]]:
        excerpts: dict[str, list[str]] = {}
        for view in views:
            seen: list[str] = []
            for observation in self.store.observations(view.resource_id):
                line = f"[{observation.evidence_id}] {observation.excerpt[:400]}"
                if observation.unit_id in view.link_units and line not in seen:
                    seen.append(line)
            excerpts[view.resource_id] = seen
        return excerpts

    @staticmethod
    def _subject_keys(subject: Subject) -> dict[str, list[str]]:
        return {"user_id": [subject.user_id], "name": [subject.name], "emails": sorted(subject.emails),
                "phones_digits": sorted(subject.phones), "email_hashes": sorted(subject.email_hashes)}

    def context(self, request: DeletionRequest) -> CaseContext:
        ctx = CaseContext(request=request, intent=classify_intent(request.request_text))
        ctx.subject = self.identities.resolve(request.email)
        if ctx.subject is not None:
            ctx.views = self.linker.resources(request, ctx.subject, self._recovered_accounts())
            ctx.decisions = self._decide(ctx.views)
        if self.adjudicator is not None and ctx.subject is not None:
            review = self.adjudicator.review(request, self._subject_keys(ctx.subject), ctx.views,
                                             self._excerpts(ctx.views))
            if review is not None:
                ctx.intent, ctx.views = review.intent, review.views
                ctx.decisions = self._decide(ctx.views)
                ctx.notes.extend(review.notes)
                if review.injection_detected:
                    ctx.notes.append("instructions found in untrusted text were ignored")
            else:
                ctx.notes.append("model review unavailable: deterministic result kept")
        return ctx

    def audit(self, request: DeletionRequest) -> tuple[CaseResult, CaseContext]:
        ctx = self.context(request)
        notes = (ctx.intent.basis, *ctx.notes)
        if not ctx.intent.is_deletion:
            return CaseResult(request.case_id, DeletionStatus.NOT_APPLICABLE, (), notes), ctx
        if ctx.subject is None:
            return CaseResult(request.case_id, DeletionStatus.INCOMPLETE, (), notes + ("subject not resolved",)), ctx
        status = status_for(ctx.decisions)
        steps = build_steps(ctx.decisions, status,
                            verify_compliant=self.policy.verify_compliant_records_when_incomplete)
        return CaseResult(request.case_id, status, tuple(steps), notes), ctx

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
