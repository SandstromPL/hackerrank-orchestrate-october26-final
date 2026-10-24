"""Operational logs in seven layouts, grouped into traces and interpreted per resource.

A trace (shared trace_id/requestId) usually opens with a lookup keyed by an identifier and
closes with the resource and its state, so the trace is the linking unit: the lookup key ties
the resource to a person even when the result line carries no identifier.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..domain import Category, EvidenceFile, Observation, parse_time
from .identifiers import USER_RE, extract_direct, is_null, normalize
from .ledgers import IDENTIFIABLE_MARKER, ledger_observation
from .records import as_bool, category_for, system_for
from .snapshot import ParsedEvidence

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
SYSLOG_RE = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)$")
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+([\w.]+):\s*(.*)$")
PIPE_RE = re.compile(r"^\|\s*(\w+)\s+\|\s+(\S+)\s+\|\s+(\S+)\s+\|\s+(\S+)\s+\|\s*(.*)$")
CLOUD_RE = re.compile(r'^requestId=(\S+)\s+level=(\w+)\s+function=(\S+)\s+message="(.*)"\s*$')

LINK_FIELDS = (
    "lookup_key", "match_key", "subject_key", "scan_key", "subject_ref", "correlation_key", "matched_value",
    "email", "customer_email", "requester_email", "owner_email", "subject_reference", "customer_reference",
)
DIRECT_FIELDS = ("email", "phone", "email_hash", "requester_email", "requester_phone", "customer_email", "owner_email")
NAME_FIELDS = ("requester_name", "display_name", "full_name")
USER_REF_FIELDS = ("customer_reference", "subject_reference", "subject_ref")
PII_SCAN_EVENTS = {"message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"}


@dataclass(frozen=True)
class LogLine:
    index: int
    time: datetime
    service: str
    trace: str
    event: str
    fields: dict[str, str]
    raw: str


def _kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in KV_RE.findall(text):
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"')
        out[key] = value
    return out


def parse_line(index: int, raw: str) -> LogLine | None:
    match = TS_RE.match(raw.strip())
    if not match:
        return None
    time, rest = parse_time(match.group(1)), match.group(2)
    service = trace = event = ""
    fields: dict[str, str]
    if rest.startswith("{"):
        try:
            data = json.loads(rest)
        except ValueError:
            data = {}
        fields = {str(k): str(v) for k, v in data.items()}
        service = fields.get("service") or fields.get("logger", "")
    elif m := SYSLOG_RE.match(rest):
        service, trace, fields = m.group(2), m.group(3), _kv(m.group(4))
    elif m := BRACKET_RE.match(rest):
        service, trace, event, fields = m.group(2), m.group(3), m.group(4), _kv(m.group(5))
    elif m := PIPE_RE.match(rest):
        service, trace, event, fields = m.group(2), m.group(3), m.group(4), _kv(m.group(5))
    elif m := CLOUD_RE.match(rest):
        trace, service, fields = m.group(1), m.group(3), _kv(m.group(4).replace('\\"', '"'))
    else:
        fields = _kv(rest)
        service = fields.get("service", "")
    trace = trace or fields.get("trace_id", "")
    event = event or fields.get("event", "")
    return LogLine(index, time, service, trace, event, fields, raw.strip())


def _exists(lines: list[LogLine]) -> bool | None:
    verdict: bool | None = None
    for line in lines:
        f = line.fields
        if line.event in PII_SCAN_EVENTS:
            verdict = True
        if f.get("http_status") in {"404", "410"}:
            return False
        if f.get("http_status") == "200":
            verdict = True
        if "rows" in f and f["rows"].isdigit():
            if f["rows"] == "0":
                return False
            verdict = True
        if f.get("result") == "miss" and line.event != "cache_lookup":
            return False
        if f.get("result") == "hit":
            verdict = True
        if f.get("total_hits", "").isdigit():
            if f["total_hits"] == "0":
                return False
            verdict = True
        if f.get("marker_found") == "true":
            return False
        if f.get("marker_found") == "false":
            verdict = True
        if f.get("available") == "false":
            return False
        if line.event.endswith(("_completed", "_read", "_loaded", "_heartbeat", "_evaluated")) and verdict is None:
            verdict = True
    return verdict


def _category(system_id: str, lines: list[LogLine]) -> Category | None:
    events = {line.event for line in lines}
    if events & {"attachment_pii_scan", "attachment_scan_started"} or any("file" in l.fields for l in lines):
        return Category.SUPPORT_ATTACHMENT
    if system_id == "analytics_platform" and any(e.startswith("contact_lookup") for e in events):
        return Category.SECONDARY_IDENTIFIER
    if any(e.startswith("contact_query") for e in events) or any(l.service == "support_replica" for l in lines):
        return Category.SECONDARY_IDENTIFIER
    return category_for(system_id)


def _flags(lines: list[LogLine]) -> tuple[bool | None, bool | None]:
    enabled = restore = None
    for line in lines:
        f = line.fields
        if "enabled" in f:
            enabled = as_bool(f["enabled"])
        if "restore_allowed" in f:
            restore = as_bool(f["restore_allowed"])
        if line.event == "restore_candidate_evaluated":
            restore = as_bool(f.get("eligible")) is True and as_bool(f.get("deletion_block")) is not True
        if line.event == "restore_authorization_completed" and "decision" in f:
            restore = f["decision"].lower() == "allow"
    return enabled, restore


def _trace_observations(evidence: EvidenceFile, unit: str, lines: list[LogLine]) -> list[Observation]:
    resource_ids: list[str] = []
    for line in lines:
        rid = line.fields.get("resource_id", "")
        if rid and rid not in resource_ids and not is_null(rid):
            resource_ids.append(rid)
    link_keys: set[str] = set()
    for line in lines:
        for name in LINK_FIELDS:
            canonical = normalize(line.fields.get(name, ""))
            if canonical:
                link_keys.add(canonical)
    observations = []
    for rid in resource_ids:
        own = [l for l in lines if l.fields.get("resource_id") in (rid, None, "")]
        system_id = system_for(rid, own[-1].service if own else "")
        direct: set[str] = set()
        refs: set[str] = set()
        for line in own:
            f = line.fields
            for name in DIRECT_FIELDS:
                canonical = normalize(f.get(name, ""))
                if canonical:
                    direct.add(canonical)
            for name in NAME_FIELDS:
                if not is_null(f.get(name)) and f.get(name) is not None:
                    direct.add("name:" + f[name].lower())
            if line.event in PII_SCAN_EVENTS and normalize(f.get("matched_value", "")):
                direct.add(normalize(f["matched_value"]) or "")
            direct |= extract_direct(f.get("message_text", "")) if not is_null(f.get("message_text")) else set()
            if f.get("direct_identifiers", "").isdigit() and int(f["direct_identifiers"]) > 0 and not direct:
                direct.add(IDENTIFIABLE_MARKER)
            for name in USER_REF_FIELDS:
                canonical = normalize(f.get(name, ""))
                if canonical and USER_RE.fullmatch(canonical):
                    refs.add(canonical)
        enabled, restore = _flags(own)
        observations.append(
            Observation(
                evidence_id=evidence.file_id,
                captured_at=evidence.captured_at,
                observed_at=max(l.time for l in own) if own else lines[-1].time,
                system_id=system_id,
                resource_id=rid,
                category=_category(system_id, own),
                exists=_exists([l for l in own if l.fields.get("resource_id") == rid] or own),
                direct_identifiers=frozenset(d for d in direct if d),
                user_refs=frozenset(refs),
                link_keys=frozenset(link_keys),
                enabled=enabled,
                restore_allowed=restore,
                excerpt="\n".join(l.raw for l in own)[:900],
                unit_id=f"{evidence.file_id}:{unit}",
            )
        )
    return observations


def parse_log(evidence: EvidenceFile, path: Path) -> ParsedEvidence:
    parsed = ParsedEvidence()
    traces: dict[str, list[LogLine]] = {}
    for index, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = parse_line(index, raw)
        if line is None:
            continue
        if line.event == "record_lookup":  # ledger-style line with explicit type and result
            observation = ledger_observation(
                evidence,
                observed_at=line.time,
                system_hint=line.fields.get("system", ""),
                resource_id=line.fields.get("resource_id", ""),
                record_type=line.fields.get("record_type", ""),
                result=line.fields.get("result", ""),
                subject_reference=line.fields.get("subject_reference", ""),
                unit_id=f"{evidence.file_id}:line{index}",
                excerpt=line.raw,
            )
            if observation:
                parsed.observations.append(observation)
            continue
        traces.setdefault(line.trace or f"line{index}", []).append(line)
    for unit, lines in traces.items():
        parsed.observations.extend(_trace_observations(evidence, unit, lines))
    return parsed
