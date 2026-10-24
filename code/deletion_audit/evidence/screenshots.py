"""Application screenshots (PNG).

Two readers: a vision-model extraction (cached JSON produced by `screenshot_reader.py`) and a
Tesseract OCR fallback. OCR garbles IDs ("support_9638" for support_9538), so in the fallback a
screenshot only yields observations for resources anchored by a data-catalog row, and the
catalog's person must be corroborated by what the screen shows.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path

from ..domain import CatalogLink, Category, EvidenceFile, Observation
from .identifiers import EMAIL_RE, RESOURCE_RE, USER_RE, extract, extract_direct, normalize
from .records import category_for, system_for
from .snapshot import ParsedEvidence

OCR_RESOURCE_RE = re.compile(
    r"\b(account|search|support|analytics|export|audit_record|backup|data_flow|tombstone|legal_hold|marketing|object)"
    r"[\s._-]?(\d{2,})\b"
)
ANCHOR_WINDOW = timedelta(hours=2)


def ocr_text(path: Path, cache_dir: Path) -> str:
    """Tesseract text for a screenshot, cached on disk; empty when Tesseract is unavailable."""
    cache = cache_dir / f"{path.stem}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    binary = shutil.which("tesseract")
    if not binary:
        return ""
    result = subprocess.run([binary, str(path), "-"], capture_output=True, text=True, timeout=120, check=False)
    text = result.stdout if result.returncode == 0 else ""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def _catalog_link_holds(link: CatalogLink, text: str) -> bool:
    """A catalog link is a claim: accept it when the screen shows the person, or shows nobody.

    A screen that shows someone else's contact details (and not the catalog person's)
    contradicts the claim, and we never act on another person's record.
    """
    lowered = text.lower()
    if link.user_id and re.search(rf"\b{re.escape(link.user_id)}\b", lowered):
        return True
    emails = [m.group(0).lower() for m in EMAIL_RE.finditer(text)]
    if any(_similar(link.email, email) for email in emails):
        return True
    return not emails and not extract_direct(text)


def _anchors(evidence: EvidenceFile, text: str, catalog: list[CatalogLink]) -> list[CatalogLink]:
    named = {f"{m.group(1)}_{m.group(2)}" for m in OCR_RESOURCE_RE.finditer(text)}
    anchors = []
    for link in catalog:
        in_text = link.resource_id in named or (link.alias and link.alias.lower() in text.lower())
        recent = timedelta(0) <= link.event_time - evidence.captured_at <= ANCHOR_WINDOW
        if in_text or recent:
            anchors.append(link)
    return anchors


def _ocr_flags(category: Category | None, text: str) -> tuple[bool | None, bool | None]:
    lowered = text.lower()
    enabled = restore = None
    if category == Category.DATA_FLOW:
        if any(word in lowered for word in ("paused", "disabled", "stopped")):
            enabled = False
        elif any(word in lowered for word in ("continuous", "active", "healthy")):
            enabled = True
    if category == Category.BACKUP_ARCHIVE:
        if any(word in lowered for word in ("restore blocked", "restore disabled", "restore denied")):
            restore = False
        elif any(word in lowered for word in ("restore allowed", "restorable", "restore available", "recovery manager")):
            restore = True  # an archive listed in a live recovery console with no block shown can be restored
    return enabled, restore


def _vision_link_holds(link: CatalogLink, identifiers: set[str]) -> bool:
    """Same rule as the OCR path: the record shows the catalog person (allowing a misread
    character), or shows nobody. A record showing only someone else contradicts the claim."""
    if not identifiers or link.user_id in identifiers or link.email in identifiers:
        return True
    return any(_similar(link.email, key) for key in identifiers if "@" in key)


def observations_from_vision(evidence: EvidenceFile, records: list[dict], catalog: list[CatalogLink]) -> ParsedEvidence:
    """Observations from the cached vision extraction (one entry per record shown on screen).

    A screen is a list view: only the record a data-catalog row anchors (by ID or display alias)
    links to a person. Other rows still contribute their state, but never create links, so a
    screen full of one person's events does not turn every row into a finding.
    """
    by_resource = {link.resource_id: link for link in catalog}
    by_alias = {link.alias.lower(): link for link in catalog if link.alias}
    parsed = ParsedEvidence()
    for index, record in enumerate(records):
        shown_id = str(record.get("resource_id") or "").strip()
        link = by_resource.get(shown_id) or by_alias.get(shown_id.lower())
        resource_id = link.resource_id if link else shown_id
        if not RESOURCE_RE.fullmatch(resource_id):
            continue  # display names and file names are not addressable records
        system_id = link.system_id if link else system_for(resource_id, str(record.get("system_id") or ""))
        identifiers = {normalize(str(v)) for v in record.get("identifiers", []) if normalize(str(v))}
        keys = {link.email, link.user_id} if link and _vision_link_holds(link, identifiers) else set()
        direct = {key for key in identifiers if not USER_RE.fullmatch(key)}
        direct |= {"name:" + str(n).lower() for n in record.get("names", []) if n}
        category = category_for(system_id)
        if system_id == "support_platform" and record.get("category") == Category.SUPPORT_ATTACHMENT.value:
            category = Category.SUPPORT_ATTACHMENT
        parsed.observations.append(
            Observation(
                evidence_id=evidence.file_id,
                captured_at=evidence.captured_at,
                observed_at=evidence.captured_at,
                system_id=system_id,
                resource_id=resource_id,
                category=category,
                exists=record.get("exists", True),
                direct_identifiers=frozenset(d for d in direct if d),
                user_refs=frozenset(k for k in identifiers if k and USER_RE.fullmatch(k)),
                link_keys=frozenset(k for k in keys if k),
                enabled=record.get("enabled"),
                restore_allowed=record.get("restore_allowed"),
                excerpt=f"[screenshot:vision] {record.get('summary', '')}"[:900],
                unit_id=f"{evidence.file_id}:vision{index}",
                linkable=bool(keys),
            )
        )
    return parsed


def parse_screenshot(
    evidence: EvidenceFile, path: Path, catalog: list[CatalogLink], cache_dir: Path,
    vision_records: list[dict] | None = None,
) -> ParsedEvidence:
    """Vision transcript when available; the OCR reading backs up any anchored record it missed."""
    ocr = _ocr_observations(evidence, path, catalog, cache_dir)
    if vision_records is None:
        return ocr
    parsed = observations_from_vision(evidence, vision_records, catalog)
    seen = {o.resource_id for o in parsed.observations}
    parsed.observations.extend(o for o in ocr.observations if o.resource_id not in seen)
    return parsed


def _ocr_observations(evidence: EvidenceFile, path: Path, catalog: list[CatalogLink], cache_dir: Path) -> ParsedEvidence:
    parsed = ParsedEvidence()
    text = ocr_text(path, cache_dir / "ocr")
    if not text:
        return parsed
    identifiers = extract(text)
    direct = extract_direct(text)
    for link in _anchors(evidence, text, catalog):
        category = category_for(link.system_id)
        enabled, restore = _ocr_flags(category, text)
        # Other rows on the same screen belong to other people, so only the catalog person links,
        # and only when the screen itself shows one of their identifiers.
        keys = {link.email, link.user_id} if _catalog_link_holds(link, text) else set()
        parsed.observations.append(
            Observation(
                evidence_id=evidence.file_id,
                captured_at=evidence.captured_at,
                observed_at=evidence.captured_at,
                system_id=link.system_id,
                resource_id=link.resource_id,
                category=category,
                exists=True,
                direct_identifiers=frozenset(direct),
                user_refs=frozenset(k for k in identifiers if USER_RE.fullmatch(k)),
                link_keys=frozenset(k for k in keys if k),
                enabled=enabled,
                restore_allowed=restore,
                excerpt="[screenshot:ocr] " + " ".join(text.split())[:900],
                unit_id=f"{evidence.file_id}:screen",
                linkable=bool(keys),
            )
        )
    return parsed
