"""Per-case orchestration: identity -> evidence -> (model review) -> policy -> steps.

The deterministic pass always runs first and is the fallback. When a model is configured it
reviews each case once (intent, ownership of soft links, categories) and code re-applies the
policy to the reviewed view. The reviews are independent, so they are sent up front on a
rate-limited thread pool; the per-case pass stays single-threaded and in case order. One failing
case never stops the run: it is logged and gets a conservative fallback row. Calls the evidence
could not settle safely (an ambiguous batch, a doubted link, a claim-based status) are recorded
per case for review_queue.csv; they never change the output row.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .activity import WorkflowBatches
from .adjudication import CaseAdjudicator
from .config import CACHE_DIR, DEFAULT_POLICY, AuditPolicy
from .dataset import Dataset, load_requests
from .domain import (Category, CaseResult, DeletionRequest, DeletionStatus, RequiredAction, ReviewItem, ReviewReason,
                     Subject, case_sort_key)
from .evidence.store import EvidenceStore
from .identity import IdentityIndex
from .intent import IntentResult, classify_intent
from .linking import BatchRecovery, CaseLinker, Recovery, ResourceView
from .llm_client import LLMClient
from .policy import Decision, decide
from .screenshot_reader import read_screenshots
from .steps import build_steps, status_for

log = logging.getLogger(__name__)
PROGRESS_EVERY = 50


@dataclass
class CaseContext:
    """Everything gathered for one case; kept for evaluation and debugging."""

    request: DeletionRequest
    intent: IntentResult
    subject: Subject | None = None
    views: list[ResourceView] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)


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
        # Backups are retained too, but their obligation (restore blocked) needs an observed restore state.
        retained = frozenset(c for c, rule in self.dataset.rules.items()
                             if rule.action == RequiredAction.RETAIN and c != Category.BACKUP_ARCHIVE)
        self.linker = CaseLinker(self.store, self.identities, self.batches, policy, retained)
        # Batch membership needs every known request (inputs only), not just the ones being audited.
        self._all_requests = [r for name in ("deletion_requests.csv", "sample_deletion_requests.csv")
                              if (dataset_root / name).exists() for r in load_requests(dataset_root / name)]
        self._recoveries: dict[str, Recovery] | None = None

    def _recovered(self) -> dict[str, Recovery]:
        """system_id -> records recovered from workflow batches, computed once over every request."""
        if self._recoveries is None:
            recoveries = []
            if self.policy.recover_accounts_from_activity:
                recoveries.append(BatchRecovery.accounts(self.linker, self.batches, self.policy.match_workflow_outcome))
            if self.policy.recover_analytics_from_activity:
                recoveries.append(BatchRecovery.analytics(self.linker, self.batches))
            pairs = []
            for request in self._all_requests if recoveries else []:
                subject = self.identities.resolve(request.email)
                if subject and classify_intent(request.request_text).is_deletion:
                    pairs.append((request, subject))
            self._recoveries = {recovery.system_id: recovery.assign(pairs) for recovery in recoveries}
        return self._recoveries

    def _assigned(self) -> dict[str, dict[str, str]]:
        return {system_id: recovery.assigned for system_id, recovery in self._recovered().items()}

    def _gaps(self, request: DeletionRequest, views: list[ResourceView]) -> list[ReviewItem]:
        """Records the subject should have that batch recovery could not pin to this request."""
        items = []
        for system_id, recovery in sorted(self._recovered().items()):
            candidates = recovery.unresolved.get(request.case_id)
            if candidates is None or any(v.system_id == system_id for v in views):
                continue  # not a claimant, or a record of this system is already in the case
            listed = ", ".join(sorted(candidates))
            rivals = recovery.rivals(request.case_id)  # resolving one of them settles the others too
            shared = f"; also open for {', '.join(rivals)}" if rivals else ""
            if system_id == "account_service":
                items.append(ReviewItem(ReviewReason.AMBIGUOUS_ACCOUNT, f"one of: {listed}{shared}") if candidates else
                             ReviewItem(ReviewReason.ACCOUNT_NOT_FOUND, "no evidence or workflow-batch row names it"))
            elif candidates:  # most requests have no analytics profile, so an empty batch says nothing
                items.append(ReviewItem(ReviewReason.AMBIGUOUS_ANALYTICS, f"possibly one of: {listed}{shared}"))
        return items

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
        rules_intent = classify_intent(request.request_text)
        ctx = CaseContext(request=request, intent=rules_intent)
        ctx.subject = self.identities.resolve(request.email)
        if ctx.subject is not None:
            ctx.views = self.linker.resources(request, ctx.subject, self._assigned())
            ctx.decisions = self._decide(ctx.views)
        if self.adjudicator is not None and ctx.subject is not None:
            review = self.adjudicator.review(request, self._subject_keys(ctx.subject), ctx.views,
                                             self._excerpts(ctx.views))
            if review is not None:
                ctx.intent, ctx.views = review.intent, review.views
                ctx.decisions = self._decide(ctx.views)
                ctx.notes.extend(review.notes)
                ctx.review += [ReviewItem(ReviewReason.RECORD_DROPPED, rid) for rid in review.dropped]
                ctx.review += [ReviewItem(ReviewReason.OWNERSHIP_DOUBTED, rid) for rid in review.doubted]
                if review.injection_detected:
                    ctx.notes.append("instructions found in untrusted text were ignored")
                    ctx.review.append(ReviewItem(ReviewReason.INJECTION_IGNORED))
                model = review.model_intent
                if model is not None and model.is_deletion != rules_intent.is_deletion:
                    ctx.review.append(ReviewItem(ReviewReason.INTENT_DISAGREEMENT,
                                                 f"rules: {rules_intent.request_type}, model: {model.request_type}, "
                                                 f"kept: {ctx.intent.basis}"))
            else:
                ctx.notes.append("model review unavailable: deterministic result kept")
                ctx.review.append(ReviewItem(ReviewReason.MODEL_UNAVAILABLE))
        return ctx

    def audit(self, request: DeletionRequest) -> tuple[CaseResult, CaseContext]:
        ctx = self.context(request)
        notes = (ctx.intent.basis, *ctx.notes)
        if not ctx.intent.is_deletion:
            return CaseResult(request.case_id, DeletionStatus.NOT_APPLICABLE, (), notes, tuple(ctx.review)), ctx
        if ctx.subject is None:
            unresolved = ReviewItem(ReviewReason.SUBJECT_NOT_RESOLVED, "no current or historical identity has this email")
            return CaseResult(request.case_id, DeletionStatus.INCOMPLETE, (), notes + ("subject not resolved",),
                              (*ctx.review, unresolved)), ctx
        claim = request.reported_status if self.policy.follow_claim_when_nothing_established else None
        status = status_for(ctx.decisions, claim)
        review = ctx.review + self._gaps(request, ctx.views)
        if claim is not None and not any(d.observed for d in ctx.decisions):
            notes += ("no record established: status follows the workflow's claim",)
            review.append(ReviewItem(ReviewReason.STATUS_FROM_CLAIM, f"reported_status {claim}"))
        steps = build_steps(ctx.decisions, status,
                            verify_compliant=self.policy.verify_compliant_records_when_incomplete)
        return CaseResult(request.case_id, status, tuple(steps), notes, tuple(review)), ctx

    def _prefetch_reviews(self, requests: list[DeletionRequest]) -> None:
        """Send every case's model review up front, up to `max_concurrency` calls at a time.

        Only the independent model calls run concurrently. This pass builds the same dossiers the
        per-case pass will; that pass then reads each answer from the client's memo, in case order,
        so every decision is still made single-threaded and the output does not depend on timing.
        """
        if self.adjudicator is None or self.client is None or self.client.max_concurrency == 1:
            return
        started, jobs = time.monotonic(), []
        for request in requests:
            try:
                subject = self.identities.resolve(request.email)
                if subject is not None:
                    views = self.linker.resources(request, subject, self._assigned())
                    jobs.append(self.adjudicator.job(request, self._subject_keys(subject), views,
                                                     self._excerpts(views)))
            except Exception:  # noqa: BLE001 - the per-case pass meets (and logs) this error again
                continue
        self.client.complete_many(jobs)
        log.info("prefetched %d model reviews in %.0fs (up to %d in flight); model answers %s", len(jobs),
                 time.monotonic() - started, self.client.max_concurrency, self.client.call_counts())

    def run(self, requests: list[DeletionRequest]) -> list[CaseResult]:
        results = []
        ordered = sorted(requests, key=lambda r: case_sort_key(r.case_id))
        self._prefetch_reviews(ordered)
        started = time.monotonic()
        for done, request in enumerate(ordered, start=1):
            try:
                result, _ = self.audit(request)
            except Exception as exc:  # noqa: BLE001 - isolation boundary: emit a fallback row and continue
                log.exception("case %s failed", request.case_id)
                result = CaseResult(request.case_id, DeletionStatus.INCOMPLETE, (), (f"error: {exc}",),
                                    (ReviewItem(ReviewReason.CASE_ERROR, type(exc).__name__),))
            results.append(result)
            if done % PROGRESS_EVERY == 0 or done == len(ordered):
                log.info("audited %d/%d cases in %.0fs; model answers %s", done, len(ordered),
                         time.monotonic() - started, self.client.call_counts() if self.client else "off")
        return results
