"""Connect evidence to a request's subject and reconstruct each record's state at audit time.

Linking and state are deliberately separate:
  * linking uses any evidence available by `audit_at` (identity is stable over time);
  * state uses only observations made inside the audit window, because evidence from before the
    request shows the pre-deletion state and says nothing about whether deletion worked.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from .activity import WorkflowBatches
from .config import AuditPolicy
from .domain import Category, DeletionRequest, Observation, Subject
from .evidence.records import category_for, system_for
from .evidence.snapshot import Coverage
from .evidence.store import EvidenceStore
from .identity import IdentityIndex


@dataclass(frozen=True)
class ResourceView:
    """Everything the policy stage needs to judge one record for one case."""

    system_id: str
    resource_id: str
    category: Category | None
    state: Observation | None  # latest observation inside the audit window
    link_basis: str  # "identifier", "related", "activity_batch"
    link_units: tuple[str, ...] = ()
    absence_proof: Coverage | None = None  # snapshot that covers the system but lacks the record

    @property
    def evidence_id(self) -> str:
        if self.state is not None:
            return self.state.evidence_id
        return self.absence_proof.evidence_id if self.absence_proof else ""


def _state_rank(observation: Observation) -> tuple:
    # Latest observation wins; file evidence beats activity checks at the same instant.
    return (observation.observed_at, not observation.evidence_id.startswith("activity_"),
            observation.captured_at, observation.evidence_id)


class CaseLinker:
    def __init__(self, store: EvidenceStore, identities: IdentityIndex, batches: WorkflowBatches,
                 policy: AuditPolicy) -> None:
        self._store = store
        self._identities = identities
        self._batches = batches
        self._policy = policy

    # ---- state -------------------------------------------------------------------------
    def _in_window(self, observation: Observation, request: DeletionRequest) -> bool:
        if observation.captured_at > request.audit_at or observation.observed_at > request.audit_at:
            return False
        return not self._policy.state_window_starts_at_request or observation.observed_at >= request.requested_at

    def state_of(self, resource_id: str, request: DeletionRequest) -> Observation | None:
        eligible = [o for o in self._store.observations(resource_id)
                    if o.exists is not None and self._in_window(o, request)]
        files = [o for o in eligible if not o.evidence_id.startswith("activity_")]
        pool = files or eligible
        return max(pool, key=_state_rank) if pool else None

    def absence_proof(self, system_id: str, resource_id: str, request: DeletionRequest) -> Coverage | None:
        proofs = [c for c in self._store.coverage
                  if c.system_id == system_id and resource_id not in c.resource_ids
                  and c.captured_at <= request.audit_at and c.observed_at <= request.audit_at
                  and (not self._policy.state_window_starts_at_request or c.observed_at >= request.requested_at)]
        return max(proofs, key=lambda c: (c.observed_at, c.evidence_id)) if proofs else None

    # ---- linking -----------------------------------------------------------------------
    def linked_resources(self, subject: Subject, cutoff: datetime) -> dict[str, tuple[str, set[str]]]:
        """resource_id -> (basis, unit ids) for every record evidence ties to the subject."""
        linked: dict[str, tuple[str, set[str]]] = {}
        for observation in self._store.linked(subject.keys, cutoff):
            basis, units = linked.get(observation.resource_id, ("identifier", set()))
            units.add(observation.unit_id)
            linked[observation.resource_id] = (basis, units)
            for related in observation.related_resources:
                if related not in linked:
                    linked[related] = ("related", {observation.unit_id})
        return linked

    def owners_of(self, resource_id: str, cutoff: datetime) -> set[str]:
        """Users whose identifiers appear next to this resource in evidence available by cutoff."""
        owners: set[str] = set()
        for observation in self._store.observations(resource_id):
            if observation.captured_at <= cutoff:
                for key in observation.link_keys | observation.user_refs:
                    owners |= self._identities.owners(key)
        return owners

    def view(self, request: DeletionRequest, resource_id: str, basis: str, units: set[str]) -> ResourceView:
        state = self.state_of(resource_id, request)
        observations = self._store.observations(resource_id)
        system_id = state.system_id if state else (observations[0].system_id if observations else system_for(resource_id))
        category = state.category if state else category_for(system_id)
        proof = self.absence_proof(system_id, resource_id, request) if state is None else None
        return ResourceView(system_id, resource_id, category, state, basis, tuple(sorted(units)), proof)

    def resources(self, request: DeletionRequest, subject: Subject,
                  recovered_accounts: dict[str, str] | None = None) -> list[ResourceView]:
        views = [self.view(request, rid, basis, units)
                 for rid, (basis, units) in sorted(self.linked_resources(subject, request.audit_at).items())]
        # A record seen only before the request (e.g. an account deleted in an earlier case) has no
        # established state now. Keep it only if it is the subject's sole account candidate.
        live = [v for v in views if v.state is not None]
        has_live_account = any(v.system_id == "account_service" for v in live)
        stale_accounts = [v for v in views if v.state is None and v.system_id == "account_service" and v.absence_proof]
        views = live + ([] if has_live_account else stale_accounts[:1])
        recovered = (recovered_accounts or {}).get(request.case_id)
        if recovered and not any(v.system_id == "account_service" for v in views):
            views.append(self.view(request, recovered, "activity_batch", set()))
        return views


class AccountRecovery:
    """Recover account IDs that no evidence ties to a person, from workflow batches.

    Every request sharing a `reported_at` is processed in one batch, so an account deleted in that
    batch belongs to one of those requests. Accounts that evidence ties to someone else are
    removed, then elimination runs like a sudoku: a request left with exactly one candidate (that
    no other request also needs) takes it. Ambiguous batches stay unassigned rather than guessed.
    """

    # The workflow's own claims must agree with each other: the run that reported COMPLETE for a
    # request is the one whose account step reported SUCCESS. Used only to pair runs with requests,
    # never as proof of a record's state.
    OUTCOME_COMPATIBLE = {
        "COMPLETE": {"SUCCESS"},
        "NOT_STARTED": {"FAILED"},
        "PARTIALLY_COMPLETE": {"PARTIAL", "SUCCESS"},
    }

    def __init__(self, linker: CaseLinker, batches: WorkflowBatches, match_workflow_outcome: bool = True) -> None:
        self._linker = linker
        self._batches = batches
        self._match_outcome = match_workflow_outcome

    def _has_live_account(self, request: DeletionRequest, subject: Subject) -> set[str]:
        linked = self._linker.linked_resources(subject, request.audit_at)
        return {rid for rid in linked
                if system_for(rid) == "account_service" and self._linker.state_of(rid, request) is not None}

    def _options(self, request: DeletionRequest, subject: Subject, taken: set[str]) -> set[str]:
        options = set()
        for row in self._batches.rows_for(request.reported_at, "account_service"):
            if row.resource_id in taken:
                continue
            if self._linker.owners_of(row.resource_id, request.audit_at) - {subject.user_id}:
                continue
            allowed = self.OUTCOME_COMPATIBLE.get(request.reported_status)
            if self._match_outcome and allowed and row.reported_result not in allowed:
                continue
            options.add(row.resource_id)
        return options

    def assign(self, requests: list[tuple[DeletionRequest, Subject]]) -> dict[str, str]:
        taken: set[str] = set()
        claimants: dict[str, tuple[DeletionRequest, Subject]] = {}
        for request, subject in requests:
            explicit = self._has_live_account(request, subject)
            taken |= explicit
            if not explicit:
                claimants[request.case_id] = (request, subject)
        options = {case_id: self._options(request, subject, taken)
                   for case_id, (request, subject) in sorted(claimants.items())}
        assigned: dict[str, str] = {}
        changed = True
        while changed:
            changed = False
            singles = {case_id: next(iter(opts)) for case_id, opts in options.items() if len(opts) == 1}
            demand: dict[str, int] = defaultdict(int)
            for account in singles.values():
                demand[account] += 1
            for case_id, account in sorted(singles.items()):
                if demand[account] == 1 and case_id in options:
                    assigned[case_id] = account
                    del options[case_id]
                    for other in options.values():
                        other.discard(account)
                    changed = True
        return assigned
