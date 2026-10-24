"""Shared output type for parsers: observations plus snapshot coverage (for absence proofs)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from ..domain import Category, EvidenceFile, Observation
from .records import category_for, facts_from_record, system_for


@dataclass(frozen=True)
class Coverage:
    """A snapshot that captured every record of `system_id` at `observed_at`.

    A resource of that system missing from `resource_ids` did not exist at that moment, which is
    how a COMPLETE case can cite a snapshot for an account the snapshot never names.
    """

    evidence_id: str
    captured_at: datetime
    observed_at: datetime
    system_id: str
    resource_ids: frozenset[str]


@dataclass
class ParsedEvidence:
    observations: list[Observation] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)

    def extend(self, other: "ParsedEvidence") -> None:
        self.observations.extend(other.observations)
        self.coverage.extend(other.coverage)


def record_observation(
    evidence: EvidenceFile,
    observed_at: datetime,
    record: dict,
    *,
    collection: str = "",
    system_hint: str = "",
    unit_id: str,
    exists: bool | None = True,
    category: Category | None = None,
) -> Observation | None:
    """Observation for one structured record that is present in a snapshot or export."""
    resource_id = str(record.get("resource_id") or "").strip()
    if not resource_id:
        return None
    system_id = system_for(resource_id, system_hint)
    facts = facts_from_record(record)
    if exists and facts.available is False:
        exists = False
    return Observation(
        evidence_id=evidence.file_id,
        captured_at=evidence.captured_at,
        observed_at=observed_at,
        system_id=system_id,
        resource_id=resource_id,
        category=category or category_for(system_id, collection),
        exists=exists,
        direct_identifiers=frozenset(facts.direct),
        user_refs=frozenset(facts.user_refs),
        link_keys=frozenset(facts.link_keys),
        enabled=facts.enabled,
        restore_allowed=facts.restore_allowed,
        excerpt=json.dumps(record, ensure_ascii=False, sort_keys=True)[:600],
        unit_id=unit_id,
        related_resources=frozenset(facts.related - {resource_id}),
    )
