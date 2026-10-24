"""Interpret structured records (SQL rows, JSON objects) into observation fields.

Field names are mapped to roles instead of writing one parser per file: a field either carries a
direct identifier (exposure), a stable user reference (allowed in redacted records), a link key,
or a state flag. This keeps the SQL and JSON parsers small and format-agnostic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..domain import Category
from .identifiers import USER_RE, extract_direct, is_null, normalize, normalize_phone, resources_in

SYSTEM_BY_PREFIX: dict[str, str] = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "analytics": "analytics_platform",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "legal_hold": "legal_service",
    "tombstone": "identity_service",
    "export": "export_service",
    "marketing": "marketing_platform",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "object": "object_storage",
}

DEFAULT_CATEGORY_BY_SYSTEM: dict[str, Category] = {
    "account_service": Category.USER_PROFILE,
    "search_service": Category.SEARCH_INDEX_ENTRY,
    "support_platform": Category.SUPPORT_MESSAGE,
    "analytics_platform": Category.ANALYTICS_PROFILE,
    "integration_service": Category.DATA_FLOW,
    "backup_service": Category.BACKUP_ARCHIVE,
    "security_service": Category.SECURITY_AUDIT_LOG,
    "legal_service": Category.LEGAL_HOLD_RECORD,
    "identity_service": Category.ACCOUNT_TOMBSTONE,
    "export_service": Category.USER_EXPORT,
    "marketing_platform": Category.MARKETING_PROFILE,
    "cache_service": Category.CACHE_ENTRY,
    "email_delivery_service": Category.EMAIL_DELIVERY_LOG,
    "object_storage": Category.SUPPORT_ATTACHMENT,
}

# Collections / tables whose name settles the category more precisely than the system does.
CATEGORY_BY_COLLECTION: dict[str, Category] = {
    "ticket_attachments": Category.SUPPORT_ATTACHMENT,
    "attachments": Category.SUPPORT_ATTACHMENT,
    "deleted_customer_references": Category.ACCOUNT_TOMBSTONE,
    "retained_order_records": Category.LEGAL_HOLD_RECORD,
}

DIRECT_FIELDS = {
    "email", "phone", "email_hash", "requester_email", "requester_phone", "owner_email", "owner_phone",
    "customer_email", "uploaded_by", "matched_value",
}
NAME_FIELDS = {"full_name", "name", "display_name", "requester_name"}
TEXT_FIELDS = {"message_text", "message_excerpt"}
USER_REF_FIELDS = {"user_id", "customer_reference", "subject_reference", "subject_selector", "subject_ref"}
LINK_ONLY_FIELDS = {"lookup_key", "match_key", "subject_key", "scan_key", "correlation_key", "query"}
RELATED_RESOURCE_FIELDS = {"source_resource_id"}


def system_for(resource_id: str, fallback: str = "") -> str:
    for prefix in sorted(SYSTEM_BY_PREFIX, key=len, reverse=True):
        if resource_id.startswith(prefix + "_"):
            return SYSTEM_BY_PREFIX[prefix]
    return fallback


def category_for(system_id: str, collection: str = "") -> Category | None:
    collection = collection.lower()
    for name, category in CATEGORY_BY_COLLECTION.items():
        if name in collection:
            return category
    return DEFAULT_CATEGORY_BY_SYSTEM.get(system_id)


def as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "enabled", "allowed"}:
            return True
        if text in {"false", "0", "no", "disabled", "denied", "deny"}:
            return False
    return None


@dataclass
class RecordFacts:
    """Role-classified content of one record."""

    direct: set[str] = field(default_factory=set)
    user_refs: set[str] = field(default_factory=set)
    link_keys: set[str] = field(default_factory=set)
    related: set[str] = field(default_factory=set)
    enabled: bool | None = None
    restore_allowed: bool | None = None
    available: bool | None = None


def _walk(record: dict, facts: RecordFacts) -> None:
    for key, value in record.items():
        name = key.lower()
        if isinstance(value, dict):
            _walk(value, facts)
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _walk(item, facts)
            continue
        if is_null(value):
            continue
        text = str(value)
        if name in DIRECT_FIELDS:
            canonical = normalize(text) or (normalize_phone(text) if name.endswith("phone") else None)
            if canonical:
                facts.direct.add(canonical)
                facts.link_keys.add(canonical)
        elif name in NAME_FIELDS:
            facts.direct.add("name:" + text.strip().lower())
        elif name in TEXT_FIELDS:
            found = extract_direct(text)
            facts.direct |= found
            facts.link_keys |= found
            facts.related |= resources_in(text)
        elif name in USER_REF_FIELDS:
            canonical = normalize(text)
            if canonical and USER_RE.fullmatch(canonical):
                facts.user_refs.add(canonical)
                facts.link_keys.add(canonical)
            elif canonical:  # an email or phone used as the subject selector is a direct identifier
                facts.direct.add(canonical)
                facts.link_keys.add(canonical)
        elif name in LINK_ONLY_FIELDS:
            canonical = normalize(text)
            if canonical:
                facts.link_keys.add(canonical)
        elif name in RELATED_RESOURCE_FIELDS:
            facts.related.add(text.strip())
        elif name == "enabled":
            facts.enabled = as_bool(value)
        elif name == "restore_allowed":
            facts.restore_allowed = as_bool(value)
        elif name in {"download_available", "available"}:
            facts.available = as_bool(value)


def facts_from_record(record: dict) -> RecordFacts:
    facts = RecordFacts()
    _walk(record, facts)
    return facts


def decode_record_json(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
