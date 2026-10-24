"""Core types shared by every pipeline stage.

Everything here is plain data: enums for the closed vocabularies in the problem statement and
frozen dataclasses for requests, subjects, evidence observations and output steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def parse_time(value: str) -> datetime:
    """Parse an ISO-8601 timestamp ("2026-09-15T12:00:00Z") into an aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def case_sort_key(case_id: str) -> tuple[str, int, str]:
    """Natural order (case_2 before case_10), matching the order of the request files."""
    prefix, _, number = case_id.rpartition("_")
    return (prefix, int(number), case_id) if number.isdigit() else (case_id, -1, case_id)


class DeletionStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Phase(str, Enum):
    FINDING = "FINDING"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"


class Operation(str, Enum):
    # Declaration order inside each phase is the required output order.
    REPORT_REMAINING_RECORD = "REPORT_REMAINING_RECORD"
    REPORT_EXPOSED_PII = "REPORT_EXPOSED_PII"
    REPORT_ACTIVE_DATA_FLOW = "REPORT_ACTIVE_DATA_FLOW"
    REPORT_RESTORE_RISK = "REPORT_RESTORE_RISK"
    DISABLE_DATA_FLOW = "DISABLE_DATA_FLOW"
    BLOCK_RESTORE = "BLOCK_RESTORE"
    DELETE_RECORD = "DELETE_RECORD"
    REDACT_RECORD = "REDACT_RECORD"
    ANONYMIZE_RECORD = "ANONYMIZE_RECORD"
    VERIFY_RECORD_ABSENT = "VERIFY_RECORD_ABSENT"
    VERIFY_RECORD_PRESENT = "VERIFY_RECORD_PRESENT"
    VERIFY_RECORD_REDACTED = "VERIFY_RECORD_REDACTED"
    VERIFY_RECORD_ANONYMIZED = "VERIFY_RECORD_ANONYMIZED"
    VERIFY_DATA_FLOW_DISABLED = "VERIFY_DATA_FLOW_DISABLED"
    VERIFY_RESTORE_BLOCKED = "VERIFY_RESTORE_BLOCKED"


OPERATION_PHASE: dict[Operation, Phase] = {
    Operation.REPORT_REMAINING_RECORD: Phase.FINDING,
    Operation.REPORT_EXPOSED_PII: Phase.FINDING,
    Operation.REPORT_ACTIVE_DATA_FLOW: Phase.FINDING,
    Operation.REPORT_RESTORE_RISK: Phase.FINDING,
    Operation.DISABLE_DATA_FLOW: Phase.REMEDIATION,
    Operation.BLOCK_RESTORE: Phase.REMEDIATION,
    Operation.DELETE_RECORD: Phase.REMEDIATION,
    Operation.REDACT_RECORD: Phase.REMEDIATION,
    Operation.ANONYMIZE_RECORD: Phase.REMEDIATION,
    Operation.VERIFY_RECORD_ABSENT: Phase.VERIFICATION,
    Operation.VERIFY_RECORD_PRESENT: Phase.VERIFICATION,
    Operation.VERIFY_RECORD_REDACTED: Phase.VERIFICATION,
    Operation.VERIFY_RECORD_ANONYMIZED: Phase.VERIFICATION,
    Operation.VERIFY_DATA_FLOW_DISABLED: Phase.VERIFICATION,
    Operation.VERIFY_RESTORE_BLOCKED: Phase.VERIFICATION,
}

OPERATION_STATE: dict[Operation, str] = {
    Operation.REPORT_REMAINING_RECORD: "PRESENT",
    Operation.REPORT_EXPOSED_PII: "PRESENT",
    Operation.REPORT_ACTIVE_DATA_FLOW: "ACTIVE",
    Operation.REPORT_RESTORE_RISK: "ACTIVE",
    Operation.DISABLE_DATA_FLOW: "DISABLED",
    Operation.BLOCK_RESTORE: "BLOCKED",
    Operation.DELETE_RECORD: "ABSENT",
    Operation.REDACT_RECORD: "REDACTED",
    Operation.ANONYMIZE_RECORD: "ANONYMIZED",
    Operation.VERIFY_RECORD_ABSENT: "ABSENT",
    Operation.VERIFY_RECORD_PRESENT: "PRESENT",
    Operation.VERIFY_RECORD_REDACTED: "REDACTED",
    Operation.VERIFY_RECORD_ANONYMIZED: "ANONYMIZED",
    Operation.VERIFY_DATA_FLOW_DISABLED: "DISABLED",
    Operation.VERIFY_RESTORE_BLOCKED: "BLOCKED",
}

PHASE_ORDER: dict[Phase, int] = {phase: index for index, phase in enumerate(Phase)}
OPERATION_ORDER: dict[Operation, int] = {op: index for index, op in enumerate(Operation)}


class Category(str, Enum):
    """Data categories from deletion_rules.csv, plus DATA_FLOW for sync/connector processes."""

    USER_PROFILE = "USER_PROFILE"
    SECONDARY_IDENTIFIER = "SECONDARY_IDENTIFIER"
    SEARCH_INDEX_ENTRY = "SEARCH_INDEX_ENTRY"
    CACHE_ENTRY = "CACHE_ENTRY"
    SUPPORT_MESSAGE = "SUPPORT_MESSAGE"
    SUPPORT_ATTACHMENT = "SUPPORT_ATTACHMENT"
    ANALYTICS_PROFILE = "ANALYTICS_PROFILE"
    PRODUCT_TELEMETRY = "PRODUCT_TELEMETRY"
    USER_EXPORT = "USER_EXPORT"
    MARKETING_PROFILE = "MARKETING_PROFILE"
    EMAIL_DELIVERY_LOG = "EMAIL_DELIVERY_LOG"
    SECURITY_AUDIT_LOG = "SECURITY_AUDIT_LOG"
    LEGAL_HOLD_RECORD = "LEGAL_HOLD_RECORD"
    BACKUP_ARCHIVE = "BACKUP_ARCHIVE"
    ACCOUNT_TOMBSTONE = "ACCOUNT_TOMBSTONE"
    DATA_FLOW = "DATA_FLOW"


class RequiredAction(str, Enum):
    DELETE = "DELETE"
    REDACT = "REDACT"
    ANONYMIZE = "ANONYMIZE"
    RETAIN = "RETAIN"
    DISABLE = "DISABLE"  # processes: an active flow must be switched off


@dataclass(frozen=True)
class DeletionRequest:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass(frozen=True)
class Subject:
    """The person behind a request, with every identifier they have ever used."""

    user_id: str
    name: str
    emails: frozenset[str]
    email_hashes: frozenset[str]
    phones: frozenset[str]

    @property
    def keys(self) -> frozenset[str]:
        """Lower-cased identifiers that link a record to this subject."""
        return frozenset({self.user_id.lower()} | self.emails | self.email_hashes | self.phones)


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    path: str
    origin: str
    captured_at: datetime


@dataclass(frozen=True)
class Observation:
    """One evidence unit's statement about one resource at one point in time.

    `captured_at` is when the evidence file became available (the citation cutoff);
    `observed_at` is when the described state held (log line time, snapshot time, ...).
    """

    evidence_id: str
    captured_at: datetime
    observed_at: datetime
    system_id: str
    resource_id: str
    category: Category | None
    exists: bool | None
    direct_identifiers: frozenset[str] = frozenset()  # emails, phones, hashes, names in the record
    user_refs: frozenset[str] = frozenset()  # stable user_id references kept in the record
    link_keys: frozenset[str] = frozenset()  # identifiers that tie the unit to a person (incl. query keys)
    enabled: bool | None = None  # data flows
    restore_allowed: bool | None = None  # backups
    implicit: bool = False  # absence inferred from a snapshot that covers the system
    excerpt: str = ""
    unit_id: str = ""
    related_resources: frozenset[str] = frozenset()
    category_stated: bool = False  # the evidence itself names the category (ledger rows)
    linkable: bool = True  # False for list-view rows that may describe state but must not create links

    @property
    def identifiable(self) -> bool:
        return bool(self.direct_identifiers)


@dataclass(frozen=True)
class ActivityEvent:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True)
class CatalogLink:
    """A data-catalog activity row that ties a resource (and its display alias) to a person."""

    activity_id: str
    event_time: datetime
    system_id: str
    resource_id: str
    alias: str
    email: str
    user_id: str
    exclusive: bool  # "belongs only to ... no other subject is linked"


@dataclass(frozen=True)
class Step:
    phase: Phase
    system_id: str
    resource_id: str
    operation: Operation
    evidence_id: str = ""

    @property
    def state(self) -> str:
        return OPERATION_STATE[self.operation]

    def sort_key(self) -> tuple[int, int, str, str]:
        return (PHASE_ORDER[self.phase], OPERATION_ORDER[self.operation], self.system_id, self.resource_id)


class ReviewReason(str, Enum):
    """Why a case is also listed in review_queue.csv: a call the evidence could not settle safely.

    Declared most severe first; `priority` groups them for triage.
    """

    CASE_ERROR = "CASE_ERROR"  # the case raised: a conservative fallback row was written
    SUBJECT_NOT_RESOLVED = "SUBJECT_NOT_RESOLVED"  # the request email matches no known identity
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"  # no usable model answer: the deterministic result stands
    INTENT_DISAGREEMENT = "INTENT_DISAGREEMENT"  # the rules and the model read the request differently
    INJECTION_IGNORED = "INJECTION_IGNORED"  # untrusted text tried to instruct the agent
    STATUS_FROM_CLAIM = "STATUS_FROM_CLAIM"  # no record established: the workflow's own claim decided
    RECORD_DROPPED = "RECORD_DROPPED"  # a soft link dropped because the record names someone else
    OWNERSHIP_DOUBTED = "OWNERSHIP_DOUBTED"  # the model doubts a soft link that no identifier refutes (kept)
    AMBIGUOUS_ACCOUNT = "AMBIGUOUS_ACCOUNT"  # the account is one of several batch rows nothing tells apart
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"  # no evidence and no batch row names the subject's account
    AMBIGUOUS_ANALYTICS = "AMBIGUOUS_ANALYTICS"  # an anonymized analytics profile in the batch may be the subject's

    @property
    def priority(self) -> int:
        """1: the status may be wrong; 2: a record may be missing or wrongly kept; 3: only a check of an
        already-compliant record may be missing."""
        if self in (ReviewReason.AMBIGUOUS_ANALYTICS,):
            return 3
        if self in (ReviewReason.RECORD_DROPPED, ReviewReason.OWNERSHIP_DOUBTED, ReviewReason.AMBIGUOUS_ACCOUNT,
                    ReviewReason.ACCOUNT_NOT_FOUND):
            return 2
        return 1


@dataclass(frozen=True)
class ReviewItem:
    reason: ReviewReason
    detail: str = ""


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: DeletionStatus
    steps: tuple[Step, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    review: tuple[ReviewItem, ...] = ()  # for review_queue.csv; never changes the output row
