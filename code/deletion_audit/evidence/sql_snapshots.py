"""SQLite-compatible snapshot exports (primary, replica, inventory and single-store captures).

Each file is executed into an in-memory SQLite database. Rows either store the record as
`record_json` (generic replica captures) or as plain columns (inventory and store captures).
A resource present in any table of the file (primary or replica) exists at capture time; the
most exposed copy wins, which is how replication drift surfaces.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ..domain import EvidenceFile, parse_time
from .records import decode_record_json, system_for
from .snapshot import Coverage, ParsedEvidence, record_observation

CAPTURED_RE = re.compile(r"--\s*Captured at:\s*(\S+)")

# Column-style tables: table name prefix -> (system, collection label).
TABLE_SYSTEMS: dict[str, tuple[str, str]] = {
    "customer_profiles": ("account_service", "customer_profiles"),
    "customer_search_documents": ("search_service", "customer_search_documents"),
    "search_documents": ("search_service", "customer_search_documents"),
    "customer_analytics_profiles": ("analytics_platform", "customer_analytics_profiles"),
    "support_records": ("support_platform", "support_records"),
    "customer_exports": ("export_service", "customer_exports"),
    "connector_subscriptions": ("integration_service", "connector_subscriptions"),
    "backup_restore_entries": ("backup_service", "backup_restore_entries"),
    "security_audit_records": ("security_service", "security_audit_records"),
}


def _table_system(table: str) -> tuple[str, str] | None:
    for prefix, value in TABLE_SYSTEMS.items():
        if table == prefix or table.startswith(prefix + "_"):
            return value
    return None


def parse_sql_snapshot(evidence: EvidenceFile, path: Path) -> ParsedEvidence:
    script = path.read_text(encoding="utf-8")
    header = CAPTURED_RE.search(script)
    file_time = parse_time(header.group(1)) if header else evidence.captured_at
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(script)
        tables = [row[0] for row in connection.execute("select name from sqlite_master where type='table'")]
        scope: dict[str, str] = {}
        if "snapshot_scope" in tables:
            scope = dict(connection.execute("select origin_system, captured_at from snapshot_scope").fetchall())
        parsed = ParsedEvidence()
        present: dict[str, set[str]] = {}
        covered: set[str] = set()
        for table in tables:
            if table == "snapshot_scope":
                continue
            cursor = connection.execute(f'select * from "{table}"')
            columns = [description[0] for description in cursor.description]
            mapped = _table_system(table)
            if mapped and (not scope or mapped[0] in scope):
                covered.add(mapped[0])
            for index, values in enumerate(cursor.fetchall()):
                row = dict(zip(columns, values))
                if "record_json" in row:
                    record = decode_record_json(row["record_json"])
                    record.setdefault("resource_id", row.get("resource_id"))
                    system_hint, collection = row.get("origin_system") or "", row.get("collection_name") or ""
                else:
                    record = {k: v for k, v in row.items() if k != "updated_at"}
                    system_hint, collection = mapped if mapped else ("", table)
                system_id = system_for(str(record.get("resource_id") or ""), system_hint)
                observed_at = parse_time(scope[system_id]) if system_id in scope else file_time
                observation = record_observation(
                    evidence,
                    observed_at,
                    record,
                    collection=collection,
                    system_hint=system_hint,
                    unit_id=f"{evidence.file_id}:{table}:{index}",
                )
                if observation:
                    parsed.observations.append(observation)
                    present.setdefault(observation.system_id, set()).add(observation.resource_id)
                    if "record_json" in row:
                        covered.add(observation.system_id)
        for system_id in sorted(covered | set(scope)):
            observed_at = parse_time(scope[system_id]) if system_id in scope else file_time
            parsed.coverage.append(
                Coverage(evidence.file_id, evidence.captured_at, observed_at, system_id,
                         frozenset(present.get(system_id, set())))
            )
        return parsed
    finally:
        connection.close()
