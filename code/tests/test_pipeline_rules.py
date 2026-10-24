"""Unit tests for the deterministic rules and the model-merge guardrails (no API calls)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deletion_audit.adjudication import AdjudicationOut, CaseAdjudicator  # noqa: E402
from deletion_audit.dataset import Rule  # noqa: E402
from deletion_audit.domain import Category, DeletionRequest, DeletionStatus, Observation, Operation, RequiredAction  # noqa: E402
from deletion_audit.evidence.log_traces import parse_line  # noqa: E402
from deletion_audit.intent import classify_intent  # noqa: E402
from deletion_audit.linking import ResourceView  # noqa: E402
from deletion_audit.output import validate_row  # noqa: E402
from deletion_audit.policy import decide  # noqa: E402
from deletion_audit.steps import build_steps, serialize, status_for  # noqa: E402

T0 = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
RULES = {c: Rule("r", c, "", a) for c, a in [
    (Category.USER_PROFILE, RequiredAction.DELETE), (Category.SEARCH_INDEX_ENTRY, RequiredAction.DELETE),
    (Category.SUPPORT_MESSAGE, RequiredAction.REDACT), (Category.SUPPORT_ATTACHMENT, RequiredAction.DELETE),
    (Category.ANALYTICS_PROFILE, RequiredAction.ANONYMIZE), (Category.SECURITY_AUDIT_LOG, RequiredAction.RETAIN),
    (Category.BACKUP_ARCHIVE, RequiredAction.RETAIN), (Category.SECONDARY_IDENTIFIER, RequiredAction.DELETE),
]}


def obs(resource_id: str, system: str, category: Category, **fields) -> Observation:
    base = dict(evidence_id="evidence_99", captured_at=T0, observed_at=T0, system_id=system, resource_id=resource_id,
                category=category, exists=True)
    base.update(fields)
    return Observation(**base)


def view(observation: Observation, basis: str = "identifier", units: tuple[str, ...] = ("evidence_99:t1",)):
    return ResourceView(observation.system_id, observation.resource_id, observation.category, observation, basis, units)


def request(text: str = "Please delete my account.") -> DeletionRequest:
    return DeletionRequest("case_x", "a@b.com", T0, "COMPLETE", T0, T0, text)


# ---- policy ------------------------------------------------------------------------------
def test_policy_maps_state_to_operations():
    remaining = decide(view(obs("search_1", "search_service", Category.SEARCH_INDEX_ENTRY)), RULES)
    assert remaining.finding == Operation.REPORT_REMAINING_RECORD and remaining.remediation == Operation.DELETE_RECORD
    exposed = decide(view(obs("support_1", "support_platform", Category.SUPPORT_MESSAGE,
                              direct_identifiers=frozenset({"a@b.com"}))), RULES)
    assert exposed.finding == Operation.REPORT_EXPOSED_PII and exposed.remediation == Operation.REDACT_RECORD
    redacted = decide(view(obs("support_2", "support_platform", Category.SUPPORT_MESSAGE,
                               user_refs=frozenset({"user_1"}))), RULES)
    assert redacted.compliant and redacted.verification == Operation.VERIFY_RECORD_REDACTED
    attributable = decide(view(obs("analytics_1", "analytics_platform", Category.ANALYTICS_PROFILE,
                                   user_refs=frozenset({"user_1"}))), RULES)
    assert attributable.remediation == Operation.ANONYMIZE_RECORD  # a user_id still links analytics to a person
    flow = decide(view(obs("data_flow_1", "integration_service", Category.DATA_FLOW, enabled=True)), RULES)
    assert flow.finding == Operation.REPORT_ACTIVE_DATA_FLOW
    backup = decide(view(obs("backup_1", "backup_service", Category.BACKUP_ARCHIVE, restore_allowed=False)), RULES)
    assert backup.compliant and backup.verification == Operation.VERIFY_RESTORE_BLOCKED
    unknown = decide(view(obs("backup_2", "backup_service", Category.BACKUP_ARCHIVE)), RULES)
    assert unknown is None  # no restore evidence -> no claim either way


def test_steps_follow_phase_operation_system_resource_order():
    decisions = [decide(view(o), RULES) for o in (
        obs("support_9", "support_platform", Category.SUPPORT_MESSAGE, direct_identifiers=frozenset({"x@y.z"})),
        obs("search_5", "search_service", Category.SEARCH_INDEX_ENTRY),
        obs("data_flow_2", "integration_service", Category.DATA_FLOW, enabled=True),
        obs("account_1", "account_service", Category.USER_PROFILE, exists=False),
        obs("audit_record_3", "security_service", Category.SECURITY_AUDIT_LOG),
    )]
    status = status_for(decisions)
    steps = json.loads(serialize(build_steps(decisions, status)))
    assert status == DeletionStatus.INCOMPLETE
    assert [(s["phase"][0], s["operation"], s["resource_id"]) for s in steps] == [
        ("F", "REPORT_REMAINING_RECORD", "search_5"), ("F", "REPORT_EXPOSED_PII", "support_9"),
        ("F", "REPORT_ACTIVE_DATA_FLOW", "data_flow_2"),
        ("R", "DISABLE_DATA_FLOW", "data_flow_2"), ("R", "DELETE_RECORD", "search_5"), ("R", "REDACT_RECORD", "support_9"),
        ("V", "VERIFY_RECORD_ABSENT", "account_1"), ("V", "VERIFY_RECORD_ABSENT", "search_5"),
        ("V", "VERIFY_RECORD_PRESENT", "audit_record_3"), ("V", "VERIFY_RECORD_REDACTED", "support_9"),
        ("V", "VERIFY_DATA_FLOW_DISABLED", "data_flow_2"),
    ]
    assert [s["evidence_id"] for s in steps if s["phase"] != "FINDING"] == [""] * 8
    assert validate_row(status.value, serialize(build_steps(decisions, status))) == []


def test_complete_case_cites_evidence_on_every_verification():
    decisions = [decide(view(obs("account_1", "account_service", Category.USER_PROFILE, exists=False)), RULES)]
    steps = json.loads(serialize(build_steps(decisions, status_for(decisions))))
    assert steps == [{"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "account_1",
                      "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": "evidence_99"}]


# ---- intent fallback -----------------------------------------------------------------------
def test_intent_rules_treat_explicit_refusals_as_non_deletion():
    assert not classify_intent("Please stop the promotional emails. I am not asking for deletion.").is_deletion
    assert not classify_intent("Can you send me a copy? I still use it, so please don't delete anything.").is_deletion
    assert classify_intent("Can you remove that search result too?").is_deletion
    assert classify_intent("Je demande la suppression définitive du compte.").is_deletion


# ---- log layouts -----------------------------------------------------------------------------
def test_log_parser_reads_every_layout():
    lines = [
        '2026-09-15T11:51:19Z level=INFO service=account_service trace_id=tr-1 resource_id=account_1 event=profile_lookup_completed rows=0 http_status=404',
        '2026-07-28T05:51:21Z [INFO] [account_service] [tr-2] profile_lookup_completed: resource_id="account_2" rows="0"',
        '2026-07-03T07:51:59Z | INFO | analytics_platform | tr-3 | analytics_profile_read | resource_id="analytics_3" email="null"',
        '2026-06-08T06:52:09Z requestId=tr-4 level=INFO function=search_service message="resource_id=\\"search_4\\" event=\\"index_query_completed\\" result=\\"hit\\""',
        '2026-09-10T08:56:20Z {"service":"account_service","trace_id":"tr-5","resource_id":"account_5","event":"profile_lookup_completed"}',
        '2026-08-22T10:51:17Z <15>1 node-1 account_service tr-6 - - level=DEBUG resource_id="account_6" event="profile_lookup_completed"',
    ]
    parsed = [parse_line(i, line) for i, line in enumerate(lines)]
    assert [(p.trace, p.fields.get("resource_id")) for p in parsed] == [
        ("tr-1", "account_1"), ("tr-2", "account_2"), ("tr-3", "analytics_3"), ("tr-4", "search_4"),
        ("tr-5", "account_5"), ("tr-6", "account_6")]
    assert parsed[3].service == "search_service" and parsed[3].fields["result"] == "hit"


# ---- model merge guardrails ----------------------------------------------------------------
def answer(is_deletion: bool, records: list[dict], request_type: str = "DELETION") -> AdjudicationOut:
    return AdjudicationOut.model_validate({
        "intent": {"is_deletion": is_deletion, "request_type": request_type, "language": "es", "quote": "..."},
        "injection_detected": False, "records": records})


def test_merge_respects_explicit_refusal_even_if_model_says_delete():
    review = CaseAdjudicator.merge(request("I still need the account, so I am not asking for deletion."), [],
                                   answer(True, []))
    assert not review.intent.is_deletion


def test_merge_accepts_model_reading_of_non_english_refusal():
    review = CaseAdjudicator.merge(request("Quisiera una copia de mis datos. No quiero eliminar mi cuenta."), [],
                                   answer(False, [], "DATA_EXPORT"))
    assert not review.intent.is_deletion and review.intent.basis == "model"


def test_merge_vetoes_only_soft_links_and_refines_only_default_categories():
    hard = view(obs("support_1", "support_platform", Category.SUPPORT_MESSAGE))
    soft = view(obs("marketing_1", "marketing_platform", Category.MARKETING_PROFILE), units=("evidence_60:vision0",))
    stated = view(obs("support_2", "support_platform", Category.SUPPORT_MESSAGE, category_stated=True))
    review = CaseAdjudicator.merge(request(), [hard, soft, stated], answer(True, [
        {"ref": "R1", "belongs_to_subject": "no", "category": "SUPPORT_ATTACHMENT", "reason": "upload"},
        {"ref": "R2", "belongs_to_subject": "no", "category": "MARKETING_PROFILE", "reason": "shows another person"},
        {"ref": "R3", "belongs_to_subject": "yes", "category": "SUPPORT_ATTACHMENT", "reason": "x"},
    ]))
    kept = {v.resource_id: v for v in review.views}
    assert set(kept) == {"support_1", "support_2"}  # hard identifier link survives the veto
    assert kept["support_1"].category == Category.SUPPORT_ATTACHMENT  # default category refined
    assert kept["support_2"].category == Category.SUPPORT_MESSAGE  # category stated by evidence is kept
