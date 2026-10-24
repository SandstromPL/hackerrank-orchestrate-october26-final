"""Turn decisions into the ordered `steps_json` contract."""

from __future__ import annotations

import json

from .domain import DeletionStatus, Phase, Step
from .policy import Decision

ALWAYS_VERIFIED_SYSTEMS = {"account_service"}


def status_for(decisions: list[Decision], claimed_status: str | None = None) -> DeletionStatus:
    """COMPLETE iff no record has a finding.

    `claimed_status` (the workflow's reported_status) is only a fallback for when no record's state
    is established at all: a COMPLETE claim stands, any other claim means INCOMPLETE. Once any
    record is established, the evidence alone decides.
    """
    if not decisions and claimed_status is not None:
        return DeletionStatus.COMPLETE if claimed_status == DeletionStatus.COMPLETE.value else DeletionStatus.INCOMPLETE
    return DeletionStatus.INCOMPLETE if any(not d.compliant for d in decisions) else DeletionStatus.COMPLETE


def build_steps(decisions: list[Decision], status: DeletionStatus, *, verify_compliant: bool = True) -> list[Step]:
    """Findings cite evidence; remediation and post-remediation checks are future work ("").

    For a COMPLETE case every step is a verification backed by the evidence of the current state.
    """
    if status == DeletionStatus.NOT_APPLICABLE:
        return []
    steps: set[Step] = set()
    for d in decisions:
        system, resource = d.view.system_id, d.view.resource_id
        if status == DeletionStatus.COMPLETE:
            if d.view.evidence_id:
                steps.add(Step(Phase.VERIFICATION, system, resource, d.verification, d.view.evidence_id))
            continue
        if d.finding:
            steps.add(Step(Phase.FINDING, system, resource, d.finding, d.view.evidence_id))
            steps.add(Step(Phase.REMEDIATION, system, resource, d.remediation))
        if d.finding or verify_compliant or system in ALWAYS_VERIFIED_SYSTEMS or d.verification.name.endswith("PRESENT"):
            steps.add(Step(Phase.VERIFICATION, system, resource, d.verification))
    return sorted(steps, key=Step.sort_key)


def serialize(steps: list[Step]) -> str:
    payload = [
        {
            "step_no": number,
            "phase": step.phase.value,
            "system_id": step.system_id,
            "resource_id": step.resource_id,
            "operation": step.operation.value,
            "state": step.state,
            "evidence_id": step.evidence_id,
        }
        for number, step in enumerate(steps, start=1)
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
