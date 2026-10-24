"""PDF business documents (statements, attachments, registers) via their text layer."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from ..domain import CatalogLink, Category, EvidenceFile, Observation
from .identifiers import USER_RE, extract, extract_direct, resources_in
from .records import category_for, system_for
from .snapshot import ParsedEvidence

ATTACHMENT_HINTS = ("attachment", "uploader", "upload history")


def pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def document_observations(
    evidence: EvidenceFile, text: str, catalog: list[CatalogLink], *, kind: str
) -> ParsedEvidence:
    """One observation per resource named in a document (directly or by a catalog alias).

    The whole document is the linking unit: a statement or attachment is about one person.
    """
    parsed = ParsedEvidence()
    resources = resources_in(text)
    resources |= {link.resource_id for link in catalog if link.alias and link.alias in text}
    identifiers = extract(text)
    direct = extract_direct(text)
    refs = {key for key in identifiers if USER_RE.fullmatch(key)}
    lowered = text.lower()
    for resource_id in sorted(resources):
        system_id = system_for(resource_id)
        category = category_for(system_id)
        if system_id == "support_platform" and any(hint in lowered for hint in ATTACHMENT_HINTS):
            category = Category.SUPPORT_ATTACHMENT
        parsed.observations.append(
            Observation(
                evidence_id=evidence.file_id,
                captured_at=evidence.captured_at,
                observed_at=evidence.captured_at,
                system_id=system_id,
                resource_id=resource_id,
                category=category,
                exists=True,
                direct_identifiers=frozenset(direct),
                user_refs=frozenset(refs),
                link_keys=frozenset(identifiers),
                excerpt=f"[{kind}] " + " ".join(text.split())[:900],
                unit_id=f"{evidence.file_id}:document",
            )
        )
    return parsed


def parse_pdf(evidence: EvidenceFile, path: Path, catalog: list[CatalogLink]) -> ParsedEvidence:
    return document_observations(evidence, pdf_text(path), catalog, kind="pdf")
