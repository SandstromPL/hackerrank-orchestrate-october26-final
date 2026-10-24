"""Identifier extraction and normalization.

Identifiers are compared in a canonical form: lower-case emails, hashes and user IDs, and
digits-only phone numbers (evidence formats phones inconsistently: "+82 10-5837-9451").
"""

from __future__ import annotations

import re
from collections.abc import Iterable

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b", re.IGNORECASE)
PHONE_RE = re.compile(r"\+\d[\d ().-]{6,}\d")
RESOURCE_RE = re.compile(
    r"\b(?:account|search|support|analytics|export|audit_record|backup|data_flow|tombstone|legal_hold"
    r"|marketing|object|cache|email_delivery)_\d+\b"
)

_NULLS = {"", "null", "none", "nil", "[redacted]", "redacted"}


def is_null(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _NULLS)


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def normalize(value: str) -> str | None:
    """Canonical form of a single identifier value, or None if it is not an identifier."""
    text = value.strip()
    if is_null(text):
        return None
    if EMAIL_RE.fullmatch(text):
        return text.lower()
    if USER_RE.fullmatch(text):
        return text.lower()
    if HASH_RE.fullmatch(text):
        return text.lower()
    if PHONE_RE.fullmatch(text):
        return normalize_phone(text)
    return None


def extract(text: str) -> set[str]:
    """All identifiers mentioned anywhere in free text (emails, user IDs, hashes, phones)."""
    if not text:
        return set()
    found = {m.group(0).lower() for m in EMAIL_RE.finditer(text)}
    found |= {m.group(0).lower() for m in USER_RE.finditer(text)}
    found |= {m.group(0).lower() for m in HASH_RE.finditer(text)}
    found |= {normalize_phone(m.group(0)) for m in PHONE_RE.finditer(text)}
    return found


def extract_direct(text: str) -> set[str]:
    """Direct identifiers in free text: emails and phones (not user IDs, not seals or hashes)."""
    if not text:
        return set()
    found = {m.group(0).lower() for m in EMAIL_RE.finditer(text)}
    found |= {normalize_phone(m.group(0)) for m in PHONE_RE.finditer(text)}
    return found


def resources_in(text: str) -> set[str]:
    return {m.group(0) for m in RESOURCE_RE.finditer(text or "")}


def normalized_all(values: Iterable[object]) -> set[str]:
    out: set[str] = set()
    for value in values:
        if isinstance(value, str):
            canonical = normalize(value)
            if canonical:
                out.add(canonical)
    return out
