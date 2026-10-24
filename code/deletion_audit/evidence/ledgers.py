"""Ledger-style rows that state category and result explicitly.

Several sources share this shape: `system | resource_id | RECORD_TYPE | RESULT | subject`
(the handover TXT, JSON ledgers, and `record_lookup` log lines).
"""

from __future__ import annotations

from pathlib import Path

from ..domain import Category, EvidenceFile, Observation, parse_time
from .identifiers import USER_RE, normalize
from .records import system_for
from .snapshot import ParsedEvidence

# RESULT -> (exists, identifiable, enabled-or-restore flag)
_RESULTS: dict[str, tuple[bool | None, bool | None, bool | None]] = {
    "FOUND": (True, None, None),
    "NOT_FOUND": (False, None, None),
    "IDENTIFIABLE": (True, True, None),
    "ANONYMIZED": (True, False, None),
    "REDACTED": (True, False, None),
    "ACTIVE": (True, None, True),
    "DISABLED": (True, None, False),
    "BLOCKED": (True, None, False),
}

IDENTIFIABLE_MARKER = "<identifiable>"


def ledger_observation(
    evidence: EvidenceFile,
    *,
    observed_at,
    system_hint: str,
    resource_id: str,
    record_type: str,
    result: str,
    subject_reference: str,
    unit_id: str,
    excerpt: str,
) -> Observation | None:
    resource_id = resource_id.strip()
    result = result.strip().upper()
    if not resource_id or result not in _RESULTS:
        return None
    exists, identifiable, flag = _RESULTS[result]
    try:
        category: Category | None = Category(record_type.strip().upper())
    except ValueError:
        category = None
    system_id = system_for(resource_id, system_hint.strip())
    key = normalize(subject_reference) if subject_reference else None
    direct: set[str] = set()
    refs: set[str] = set()
    if key and USER_RE.fullmatch(key):
        refs.add(key)
    elif key and identifiable:
        direct.add(key)
    if identifiable and not direct:
        direct.add(IDENTIFIABLE_MARKER)
    return Observation(
        evidence_id=evidence.file_id,
        captured_at=evidence.captured_at,
        observed_at=observed_at,
        system_id=system_id,
        resource_id=resource_id,
        category=category,
        exists=exists,
        direct_identifiers=frozenset(direct),
        user_refs=frozenset(refs),
        link_keys=frozenset({key} if key else set()),
        enabled=flag if category == Category.DATA_FLOW else None,
        restore_allowed=flag if category == Category.BACKUP_ARCHIVE else None,
        excerpt=excerpt[:600],
        unit_id=unit_id,
        category_stated=category is not None,
    )


def parse_text_ledger(evidence: EvidenceFile, path: Path) -> ParsedEvidence:
    """Plain-text handover: `- <time> | <system> | <resource> | <TYPE> | <RESULT> | <subject>`."""
    parsed = ParsedEvidence()
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        parts = [part.strip() for part in line.strip().lstrip("-").split("|")]
        if len(parts) != 6:
            continue
        try:
            observed_at = parse_time(parts[0])
        except ValueError:
            observed_at = evidence.captured_at
        observation = ledger_observation(
            evidence,
            observed_at=observed_at,
            system_hint=parts[1],
            resource_id=parts[2],
            record_type=parts[3],
            result=parts[4],
            subject_reference=parts[5],
            unit_id=f"{evidence.file_id}:line{index}",
            excerpt=line.strip(),
        )
        if observation:
            parsed.observations.append(observation)
    return parsed
