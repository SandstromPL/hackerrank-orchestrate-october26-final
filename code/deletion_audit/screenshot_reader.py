"""One vision call per screenshot, transcribing the records it shows (cached like every call).

OCR misreads IDs and emails ("support_9638", "areedt433"), and exact IDs are what the audit cites,
so screenshots are read by the model once and the transcription feeds the normal evidence parser.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .dataset import Dataset
from .llm_client import LLMClient

log = logging.getLogger(__name__)

PROMPT_VERSION = "screenshot-v3"
SCHEMA: dict = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string"},
                    "system_id": {"type": "string"},
                    "category": {"type": "string"},
                    "identifiers": {"type": "array", "items": {"type": "string"}},
                    "names": {"type": "array", "items": {"type": "string"}},
                    "exists": {"type": "boolean"},
                    "enabled": {"type": ["boolean", "null"]},
                    "restore_allowed": {"type": ["boolean", "null"]},
                    "summary": {"type": "string"},
                },
                "required": ["resource_id", "system_id", "category", "identifiers", "names", "exists", "enabled",
                             "restore_allowed", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["records"],
    "additionalProperties": False,
}


class ScreenRecord(BaseModel):
    resource_id: str
    system_id: str
    category: str
    identifiers: list[str]
    names: list[str]
    exists: bool
    enabled: bool | None
    restore_allowed: bool | None
    summary: str


class ScreenTranscript(BaseModel):
    records: list[ScreenRecord]


def read_screenshots(client: LLMClient, dataset: Dataset) -> dict[str, list[dict]]:
    """file_id -> transcribed records, for every PNG in the manifest that the model read."""
    system = (Path(__file__).parent / "prompts" / "screenshot.md").read_text(encoding="utf-8")
    screenshots = [e for e in dataset.manifest if (dataset.root / e.path).suffix.lower() == ".png"]
    jobs = []
    for evidence in screenshots:
        image = base64.standard_b64encode((dataset.root / evidence.path).read_bytes()).decode("ascii")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}},
            {"type": "text", "text": f"Screenshot {evidence.file_id} from {evidence.origin}, captured "
                                     f"{evidence.captured_at.isoformat()}. Transcribe its records."},
        ]
        jobs.append(dict(purpose="screenshot", version=PROMPT_VERSION, system=system, content=content,
                         schema=SCHEMA, validate=ScreenTranscript.model_validate))
    transcripts: dict[str, list[dict]] = {}
    for evidence, raw in zip(screenshots, client.complete_many(jobs)):
        if raw is None:
            continue
        try:
            transcripts[evidence.file_id] = [r.model_dump() for r in ScreenTranscript.model_validate(raw).records]
        except ValidationError as exc:
            log.warning("screenshot %s transcript failed validation: %s", evidence.file_id, exc)
    return transcripts
