"""Loaders for the participant-facing CSV files in dataset/."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .domain import ActivityEvent, Category, DeletionRequest, EvidenceFile, RequiredAction, parse_time


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_requests(path: Path) -> list[DeletionRequest]:
    return [
        DeletionRequest(
            case_id=row["case_id"],
            email=row["email"].strip(),
            requested_at=parse_time(row["requested_at"]),
            reported_status=row["reported_status"],
            reported_at=parse_time(row["reported_at"]),
            audit_at=parse_time(row["audit_at"]),
            request_text=row["request_text"],
        )
        for row in _read_csv(path)
    ]


@dataclass(frozen=True)
class IdentityRow:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str


@dataclass(frozen=True)
class AuditLogRow:
    audit_id: str
    user_id: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: Category
    condition: str
    action: RequiredAction


def load_identities(path: Path) -> list[IdentityRow]:
    return [
        IdentityRow(r["user_id"], r["name"], r["email"], r["phone"], r["email_hash"]) for r in _read_csv(path)
    ]


def load_audit_log(path: Path) -> list[AuditLogRow]:
    return [
        AuditLogRow(r["audit_id"], r["user_id"], r["field_name"], r["old_value"], r["new_value"])
        for r in _read_csv(path)
    ]


def load_rules(path: Path) -> dict[Category, Rule]:
    rules: dict[Category, Rule] = {}
    for row in _read_csv(path):
        category = Category(row["data_category"])
        rules[category] = Rule(row["rule_id"], category, row["condition"], RequiredAction(row["required_action"]))
    return rules


def load_activity(path: Path) -> list[ActivityEvent]:
    return [
        ActivityEvent(
            activity_id=r["activity_id"],
            event_time=parse_time(r["event_time"]),
            event_type=r["event_type"],
            source_system=r["source_system"],
            target_system=r["target_system"],
            resource_id=r["resource_id"],
            reported_result=r["reported_result"],
            details=r["details"],
        )
        for r in _read_csv(path)
    ]


def load_manifest(path: Path) -> list[EvidenceFile]:
    return [
        EvidenceFile(r["file_id"], r["file_path"], r["origin"], parse_time(r["captured_at"])) for r in _read_csv(path)
    ]


@dataclass(frozen=True)
class Dataset:
    root: Path
    identities: list[IdentityRow]
    audit_log: list[AuditLogRow]
    rules: dict[Category, Rule]
    activity: list[ActivityEvent]
    manifest: list[EvidenceFile]

    @classmethod
    def load(cls, root: Path) -> "Dataset":
        return cls(
            root=root,
            identities=load_identities(root / "user_identities.csv"),
            audit_log=load_audit_log(root / "user_audit_log.csv"),
            rules=load_rules(root / "deletion_rules.csv"),
            activity=load_activity(root / "system_activity.csv"),
            manifest=load_manifest(root / "evidence_files.csv"),
        )
