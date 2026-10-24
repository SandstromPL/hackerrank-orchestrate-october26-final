"""System activity: claims, catalog links, state checks and workflow batches.

Operation rows (DELETE_RECORD ... SUCCESS) are claims and never prove state. Three kinds of rows
are still useful:
  * LINK_RESOURCE_IDENTITY: the data catalog ties a resource (and its display alias) to a person;
  * CHECK_* and "repopulated" rows: point-in-time observations, used only when no file covers them;
  * the requested operation type, which reveals the treatment the workflow intended for a record.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta

from .domain import ActivityEvent, CatalogLink, Category, Observation, RequiredAction
from .evidence.identifiers import normalize
from .evidence.records import category_for, system_for

CATALOG_RE = re.compile(
    r"(?:Data|Attachment) catalog (?:linked|confirmed) (?P<rid>[\w]+) \((?P<alias>[^)]+)\) "
    r"(?:to|belongs only to) (?P<email>\S+@\S+?) and (?P<user>user_\d+)"
)
REPOPULATED_RE = re.compile(r"repopulated downstream resource (?P<rid>[\w]+)")

OPERATION_ACTIONS = {
    "DELETE_RECORD": RequiredAction.DELETE,
    "REDACT_RECORD": RequiredAction.REDACT,
    "ANONYMIZE_RECORD": RequiredAction.ANONYMIZE,
    "RETAIN_RECORD": RequiredAction.RETAIN,
}
WORKFLOW_ACTORS = {"deletion_worker"}


def catalog_links(activity: list[ActivityEvent]) -> list[CatalogLink]:
    links = []
    for event in activity:
        if event.event_type != "LINK_RESOURCE_IDENTITY":
            continue
        match = CATALOG_RE.search(event.details)
        if not match:
            continue
        links.append(
            CatalogLink(
                activity_id=event.activity_id,
                event_time=event.event_time,
                system_id=system_for(match.group("rid"), event.target_system),
                resource_id=match.group("rid"),
                alias=match.group("alias"),
                email=(normalize(match.group("email").rstrip(".;")) or ""),
                user_id=match.group("user").lower(),
                exclusive="belongs only to" in event.details,
            )
        )
    return links


def check_observations(activity: list[ActivityEvent]) -> list[Observation]:
    """Activity rows that describe an observed state rather than an operation outcome."""
    observations = []
    for event in activity:
        base = dict(evidence_id=event.activity_id, captured_at=event.event_time, observed_at=event.event_time,
                    excerpt=f"{event.activity_id} {event.event_type}: {event.details}",
                    unit_id=event.activity_id)
        if event.event_type == "CHECK_DATA_FLOW" and "remains enabled" in event.details:
            observations.append(Observation(system_id="integration_service", resource_id=event.resource_id,
                                            category=Category.DATA_FLOW, exists=True, enabled=True, **base))
        elif event.event_type == "CHECK_RESTORE_ACCESS" and "can still be restored" in event.details:
            observations.append(Observation(system_id="backup_service", resource_id=event.resource_id,
                                            category=Category.BACKUP_ARCHIVE, exists=True, restore_allowed=True,
                                            **base))
        elif (match := REPOPULATED_RE.search(event.details)) and event.reported_result == "SUCCESS":
            source_category = Category.BACKUP_ARCHIVE if event.event_type == "RESTORE_BACKUP" else Category.DATA_FLOW
            observations.append(Observation(
                system_id=system_for(event.resource_id, event.source_system), resource_id=event.resource_id,
                category=source_category, exists=True,
                enabled=True if source_category == Category.DATA_FLOW else None,
                restore_allowed=True if source_category == Category.BACKUP_ARCHIVE else None,
                related_resources=frozenset({match.group("rid")}), **base))
            target = match.group("rid")
            target_system = system_for(target, event.target_system)
            observations.append(Observation(system_id=target_system, resource_id=target,
                                            category=category_for(target_system), exists=True, **base))
    return observations


def intended_actions(activity: list[ActivityEvent]) -> dict[str, RequiredAction]:
    """Treatment the deletion workflow requested for each resource (latest request wins)."""
    intended: dict[str, RequiredAction] = {}
    for event in sorted(activity, key=lambda e: e.event_time):
        if event.source_system in WORKFLOW_ACTORS and event.event_type in OPERATION_ACTIONS:
            intended[event.resource_id] = OPERATION_ACTIONS[event.event_type]
    return intended


def non_deletion_classifications(activity: list[ActivityEvent]) -> list[ActivityEvent]:
    return [e for e in activity if e.event_type == "CLASSIFY_REQUEST" and "non-deletion" in e.details]


class WorkflowBatches:
    """Deletion-worker rows grouped by the time window of each request's workflow run.

    The workflow processes every request that shares a `reported_at` in one batch shortly before
    that time, so its rows are the only trace of records (often accounts) whose evidence no longer
    names the person.
    """

    # Samples show batch rows 45-75 min before reported_at (generated runs) or within ±10 min (manual
    # runs); a wider window pulls in the neighbouring hourly batches.
    def __init__(self, activity: list[ActivityEvent], before: timedelta = timedelta(minutes=80),
                 after: timedelta = timedelta(minutes=10)) -> None:
        self._rows = sorted((e for e in activity if e.source_system in WORKFLOW_ACTORS),
                            key=lambda e: (e.event_time, e.activity_id))
        self._before, self._after = before, after

    def rows_for(self, reported_at: datetime, target_system: str | None = None) -> list[ActivityEvent]:
        start, end = reported_at - self._before, reported_at + self._after
        return [e for e in self._rows
                if start <= e.event_time <= end and (target_system is None or e.target_system == target_system)]


def resources_by_target(rows: list[ActivityEvent]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row.resource_id not in grouped[row.target_system]:
            grouped[row.target_system].append(row.resource_id)
    return grouped
