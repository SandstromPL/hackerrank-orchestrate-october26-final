"""Load every evidence file once and index the resulting observations."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..activity import catalog_links, check_observations
from ..dataset import Dataset
from ..domain import CatalogLink, Observation
from .documents import parse_pdf
from .json_exports import parse_json_evidence
from .ledgers import parse_text_ledger
from .log_traces import parse_log
from .screenshots import parse_screenshot
from .snapshot import Coverage, ParsedEvidence
from .sql_snapshots import parse_sql_snapshot

log = logging.getLogger(__name__)


def available(observation: Observation, cutoff: datetime, by_information_time: bool = False) -> bool:
    """Can this observation link a record by the cutoff? By default its file must be captured by
    then; by information time, a snapshot taken by then counts even inside a later export."""
    return (observation.observed_at if by_information_time else observation.captured_at) <= cutoff


class EvidenceStore:
    def __init__(self, dataset: Dataset, cache_dir: Path, vision: dict[str, list[dict]] | None = None) -> None:
        self.catalog: list[CatalogLink] = catalog_links(dataset.activity)
        self.files = {f.file_id: f for f in dataset.manifest}
        self.errors: dict[str, str] = {}
        parsed = ParsedEvidence()
        for evidence in dataset.manifest:
            path = dataset.root / evidence.path
            try:
                suffix = path.suffix.lower()
                if suffix == ".sql":
                    parsed.extend(parse_sql_snapshot(evidence, path))
                elif suffix == ".json":
                    parsed.extend(parse_json_evidence(evidence, path))
                elif suffix == ".log":
                    parsed.extend(parse_log(evidence, path))
                elif suffix == ".txt":
                    parsed.extend(parse_text_ledger(evidence, path))
                elif suffix == ".pdf":
                    parsed.extend(parse_pdf(evidence, path, self.catalog))
                elif suffix == ".png":
                    records = (vision or {}).get(evidence.file_id)
                    parsed.extend(parse_screenshot(evidence, path, self.catalog, cache_dir, records))
                else:
                    self.errors[evidence.file_id] = f"unsupported file type {suffix}"
            except Exception as exc:  # noqa: BLE001 - isolation boundary: one bad file never stops the run
                self.errors[evidence.file_id] = f"{type(exc).__name__}: {exc}"
                log.warning("evidence %s failed to parse: %s", evidence.file_id, exc)
        self.file_observations: list[Observation] = parsed.observations
        self.activity_observations: list[Observation] = check_observations(dataset.activity)
        self.coverage: list[Coverage] = parsed.coverage
        self._by_resource: dict[str, list[Observation]] = defaultdict(list)
        self._by_key: dict[str, list[Observation]] = defaultdict(list)
        for observation in self.file_observations + self.activity_observations:
            self._by_resource[observation.resource_id].append(observation)
            if observation.linkable:
                for key in observation.link_keys | observation.user_refs:
                    self._by_key[key].append(observation)

    def observations(self, resource_id: str) -> list[Observation]:
        return self._by_resource.get(resource_id, [])

    def linked(self, keys: frozenset[str], available_by: datetime,
               by_information_time: bool = False) -> list[Observation]:
        """Observations whose unit mentions any key, from evidence available by the cutoff."""
        seen: dict[tuple[str, str], Observation] = {}
        for key in sorted(keys):
            for observation in self._by_key.get(key, []):
                if available(observation, available_by, by_information_time):
                    seen[(observation.unit_id, observation.resource_id)] = observation
        return list(seen.values())
