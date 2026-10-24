"""JSON evidence: snapshot bundles, service resource exports, collection dumps and ledgers."""

from __future__ import annotations

import json
from pathlib import Path

from ..domain import EvidenceFile, parse_time
from .ledgers import ledger_observation
from .records import system_for
from .snapshot import Coverage, ParsedEvidence, record_observation


def _time(value: object, fallback):
    try:
        return parse_time(str(value)) if value else fallback
    except ValueError:
        return fallback


def _snapshot_records(evidence: EvidenceFile, snapshot: dict, base_time, unit_prefix: str) -> ParsedEvidence:
    parsed = ParsedEvidence()
    observed_at = _time(snapshot.get("captured_at"), base_time)
    store = str(snapshot.get("data_store") or "")
    collection = str(snapshot.get("collection") or "")
    present: set[str] = set()
    for index, record in enumerate(snapshot.get("records") or []):
        if not isinstance(record, dict):
            continue
        observation = record_observation(
            evidence, observed_at, record, collection=collection, system_hint=store, unit_id=f"{unit_prefix}:{index}"
        )
        if observation:
            parsed.observations.append(observation)
            present.add(observation.resource_id)
    system_id = store or (system_for(next(iter(present))) if present else "")
    if system_id:
        parsed.coverage.append(Coverage(evidence.file_id, evidence.captured_at, observed_at, system_id, frozenset(present)))
    return parsed


def _service_export(evidence: EvidenceFile, payload: dict) -> ParsedEvidence:
    """`service_exports: {service: {group: [items]}}` read-back exports."""
    parsed = ParsedEvidence()
    base_time = _time(payload.get("exported_at"), evidence.captured_at)
    for service, groups in sorted((payload.get("service_exports") or {}).items()):
        for group, items in sorted((groups or {}).items()):
            for index, item in enumerate(items or []):
                if not isinstance(item, dict):
                    continue
                unit_id = f"{evidence.file_id}:{service}.{group}:{index}"
                observed_at = _time(item.get("observed_at"), base_time)
                if "status_code" in item:  # profile lookups: 404 means the profile is gone
                    record = dict(item.get("profile") or {})
                    record["resource_id"] = item.get("resource_id")
                    exists = str(item.get("status_code")) == "200"
                    observation = record_observation(evidence, observed_at, record, system_hint=service,
                                                     collection=group, unit_id=unit_id, exists=exists)
                    if observation:
                        parsed.observations.append(observation)
                    continue
                if "hits" in item:  # search queries: each hit is a live document
                    hits = item.get("hits") or {}
                    documents = hits.get("documents") or []
                    for doc_index, document in enumerate(documents):
                        record = dict(document)
                        record["query"] = item.get("query")
                        observation = record_observation(evidence, observed_at, record, system_hint=service,
                                                         collection=group, unit_id=f"{unit_id}:{doc_index}")
                        if observation:
                            parsed.observations.append(observation)
                    if not documents and item.get("resource_id"):
                        record = {"resource_id": item.get("resource_id"), "query": item.get("query")}
                        observation = record_observation(evidence, observed_at, record, system_hint=service,
                                                         collection=group, unit_id=unit_id, exists=False)
                        if observation:
                            parsed.observations.append(observation)
                    continue
                observation = record_observation(evidence, observed_at, item, system_hint=service,
                                                 collection=group, unit_id=unit_id)
                if observation:
                    parsed.observations.append(observation)
    return parsed


def parse_json_evidence(evidence: EvidenceFile, path: Path) -> ParsedEvidence:
    payload = json.loads(path.read_text(encoding="utf-8"))
    parsed = ParsedEvidence()
    if not isinstance(payload, dict):
        return parsed
    base_time = _time(payload.get("exported_at") or payload.get("captured_at"), evidence.captured_at)
    if isinstance(payload.get("snapshots"), list):
        for index, snapshot in enumerate(payload["snapshots"]):
            if isinstance(snapshot, dict):
                parsed.extend(_snapshot_records(evidence, snapshot, base_time, f"{evidence.file_id}:snapshot{index}"))
    elif isinstance(payload.get("service_exports"), dict):
        parsed.extend(_service_export(evidence, payload))
    elif isinstance(payload.get("records"), list):
        records = payload["records"]
        if records and isinstance(records[0], dict) and "record_type" in records[0]:
            for index, record in enumerate(records):
                observation = ledger_observation(
                    evidence,
                    observed_at=_time(record.get("recorded_at"), base_time),
                    system_hint=str(record.get("system") or ""),
                    resource_id=str(record.get("resource_id") or ""),
                    record_type=str(record.get("record_type") or ""),
                    result=str(record.get("result") or ""),
                    subject_reference=str(record.get("subject_reference") or ""),
                    unit_id=f"{evidence.file_id}:record{index}",
                    excerpt=json.dumps(record, ensure_ascii=False, sort_keys=True),
                )
                if observation:
                    parsed.observations.append(observation)
        else:
            snapshot = {"captured_at": payload.get("captured_at"), "collection": payload.get("collection"),
                        "data_store": "", "records": records}
            parsed.extend(_snapshot_records(evidence, snapshot, base_time, f"{evidence.file_id}:records"))
    return parsed
