"""Apply deletion_rules.csv to each record's state: compliant, or a finding with its fix.

This is the only place that maps (category, state) to operations, so every output step can be
traced to one rule and one observation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .dataset import Rule
from .domain import Category, Operation, RequiredAction
from .linking import ResourceView


@dataclass(frozen=True)
class Decision:
    view: ResourceView
    action: RequiredAction
    finding: Operation | None  # None when the record already has the required treatment
    remediation: Operation | None
    verification: Operation
    observed: bool = True  # False for a plan-only check of a record no in-window evidence shows

    @property
    def compliant(self) -> bool:
        return self.finding is None


def required_action(category: Category | None, rules: dict[Category, Rule]) -> RequiredAction | None:
    if category == Category.DATA_FLOW:
        return RequiredAction.DISABLE
    rule = rules.get(category) if category else None
    return rule.action if rule else None


def decide(view: ResourceView, rules: dict[Category, Rule]) -> Decision | None:
    """Decision for one record, or None when the evidence does not establish its state."""
    action = required_action(view.category, rules)
    state = view.state
    if action is None:
        return None
    if view.category == Category.DATA_FLOW:
        if state is None or state.enabled is None:
            return None
        if state.enabled:
            return Decision(view, action, Operation.REPORT_ACTIVE_DATA_FLOW, Operation.DISABLE_DATA_FLOW,
                            Operation.VERIFY_DATA_FLOW_DISABLED)
        return Decision(view, action, None, None, Operation.VERIFY_DATA_FLOW_DISABLED)
    if view.category == Category.BACKUP_ARCHIVE:
        # Backups are retained (rule_14); the obligation is that restore access stays blocked.
        if state is None or state.restore_allowed is None:
            return None
        if state.restore_allowed:
            return Decision(view, action, Operation.REPORT_RESTORE_RISK, Operation.BLOCK_RESTORE,
                            Operation.VERIFY_RESTORE_BLOCKED)
        return Decision(view, action, None, None, Operation.VERIFY_RESTORE_BLOCKED)
    exists = state.exists if state else (False if view.absence_proof or view.link_basis == "activity_batch" else None)
    if action == RequiredAction.DELETE:
        if exists is None:
            return None
        if exists:
            return Decision(view, action, Operation.REPORT_REMAINING_RECORD, Operation.DELETE_RECORD,
                            Operation.VERIFY_RECORD_ABSENT)
        return Decision(view, action, None, None, Operation.VERIFY_RECORD_ABSENT)
    if action == RequiredAction.RETAIN and state is None:
        # Linked but not observed in the audit window: still policy-required, so the plan verifies it
        # remains (if it is gone, that check is what fails; no operation restores it). Snapshots that
        # merely omit it prove nothing for a retained record. build_steps lists it only in INCOMPLETE
        # plans, because a COMPLETE case has nothing to cite.
        return Decision(view, action, None, None, Operation.VERIFY_RECORD_PRESENT, observed=False)
    if not exists or state is None:
        return None  # a retained/redacted/anonymized record that is gone has nothing left to verify
    if action == RequiredAction.RETAIN:
        return Decision(view, action, None, None, Operation.VERIFY_RECORD_PRESENT)
    if action == RequiredAction.REDACT:
        # A stable user_id reference may remain (tombstones, retained conversations).
        if state.identifiable:
            return Decision(view, action, Operation.REPORT_EXPOSED_PII, Operation.REDACT_RECORD,
                            Operation.VERIFY_RECORD_REDACTED)
        return Decision(view, action, None, None, Operation.VERIFY_RECORD_REDACTED)
    if action == RequiredAction.ANONYMIZE:
        # Anonymized means no route back to the person, so a user_id reference also counts.
        if state.identifiable or state.user_refs:
            return Decision(view, action, Operation.REPORT_EXPOSED_PII, Operation.ANONYMIZE_RECORD,
                            Operation.VERIFY_RECORD_ANONYMIZED)
        return Decision(view, action, None, None, Operation.VERIFY_RECORD_ANONYMIZED)
    return None
