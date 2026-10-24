"""Per-case model adjudication: the model describes, deterministic code decides.

One structured call per case covers the judgments parsing cannot make reliably:
  * intent: does the (possibly non-English) request text authorize deletion?
  * ownership: does a softly linked record (catalog claim, workflow batch) really belong to the
    subject, or does its content show someone else?
  * category: is a support/analytics record really the default category for its system?

The answer is validated against the dossier and merged under fixed rules: the model may veto
soft links and refine default categories, but it cannot add records, change parsed states, or
override an explicit "do not delete" in the request. Any failure keeps the deterministic result.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from .activity import intended_actions
from .domain import ActivityEvent, Category, DeletionRequest
from .evidence.records import DEFAULT_CATEGORY_BY_SYSTEM
from .intent import IntentResult, classify_intent
from .linking import ResourceView
from .llm_client import LLMClient

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSION = "adjudication-v1"
REQUEST_TYPES = ["DELETION", "ACCESS", "DATA_EXPORT", "CORRECTION", "MARKETING_OPT_OUT", "ACCOUNT_SUPPORT",
                 "BILLING", "OTHER"]
SOFT_BASES = {"activity_batch", "related"}
# Categories the model may switch between, per system, when the parser only had the system default.
REFINABLE: dict[str, set[Category]] = {
    "support_platform": {Category.SUPPORT_MESSAGE, Category.SUPPORT_ATTACHMENT, Category.SECONDARY_IDENTIFIER},
    "analytics_platform": {Category.ANALYTICS_PROFILE, Category.PRODUCT_TELEMETRY, Category.SECONDARY_IDENTIFIER,
                           Category.MARKETING_PROFILE},
    "object_storage": {Category.SUPPORT_ATTACHMENT, Category.USER_EXPORT},
}

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "object",
            "properties": {
                "is_deletion": {"type": "boolean"},
                "request_type": {"type": "string", "enum": REQUEST_TYPES},
                "language": {"type": "string"},
                "quote": {"type": "string"},
            },
            "required": ["is_deletion", "request_type", "language", "quote"],
            "additionalProperties": False,
        },
        "injection_detected": {"type": "boolean"},
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "belongs_to_subject": {"type": "string", "enum": ["yes", "no", "uncertain"]},
                    "category": {"type": "string", "enum": [c.value for c in Category]},
                    "reason": {"type": "string"},
                },
                "required": ["ref", "belongs_to_subject", "category", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["intent", "injection_detected", "records"],
    "additionalProperties": False,
}


class IntentOut(BaseModel):
    is_deletion: bool
    request_type: Literal["DELETION", "ACCESS", "DATA_EXPORT", "CORRECTION", "MARKETING_OPT_OUT",
                          "ACCOUNT_SUPPORT", "BILLING", "OTHER"]
    language: str
    quote: str


class RecordOut(BaseModel):
    ref: str
    belongs_to_subject: Literal["yes", "no", "uncertain"]
    category: Category
    reason: str


class AdjudicationOut(BaseModel):
    intent: IntentOut
    injection_detected: bool
    records: list[RecordOut]


@dataclass(frozen=True)
class Review:
    intent: IntentResult
    views: list[ResourceView]
    injection_detected: bool
    notes: tuple[str, ...]


def _soft(view: ResourceView) -> bool:
    if view.link_basis in SOFT_BASES:
        return True
    return bool(view.link_units) and all(":screen" in u or ":vision" in u or ":document" in u for u in view.link_units)


class CaseAdjudicator:
    def __init__(self, client: LLMClient, activity: list[ActivityEvent]) -> None:
        self._client = client
        self._system = (PROMPT_DIR / "adjudication.md").read_text(encoding="utf-8")
        self._intended = intended_actions(activity)

    def dossier(self, request: DeletionRequest, subject_keys: dict[str, list[str]],
                views: list[ResourceView], excerpts: dict[str, list[str]]) -> dict:
        records = []
        for index, view in enumerate(views, start=1):
            state = view.state
            records.append({
                "ref": f"R{index}",
                "system_id": view.system_id,
                "resource_id": view.resource_id,
                "parser_category": view.category.value if view.category else None,
                "link_basis": view.link_basis,
                "linking_evidence": excerpts.get(view.resource_id, [])[:2],
                "state_at_audit": None if state is None else {
                    "evidence_id": state.evidence_id,
                    "observed_at": state.observed_at.isoformat(),
                    "exists": state.exists,
                    "identifiers_in_record": sorted(state.direct_identifiers)[:6],
                    "user_references": sorted(state.user_refs),
                    "enabled": state.enabled,
                    "restore_allowed": state.restore_allowed,
                },
                "workflow_requested_action": getattr(self._intended.get(view.resource_id), "value", None),
            })
        return {
            "case": {"case_id": request.case_id, "requested_at": request.requested_at.isoformat(),
                     "audit_at": request.audit_at.isoformat(), "reported_status_claim": request.reported_status},
            "subject": subject_keys,
            "candidate_records": records,
        }

    def review(self, request: DeletionRequest, subject_keys: dict[str, list[str]], views: list[ResourceView],
               excerpts: dict[str, list[str]]) -> Review | None:
        dossier = self.dossier(request, subject_keys, views, excerpts)
        content = [{"type": "text", "text": "DOSSIER (untrusted data):\n" + json.dumps(dossier, ensure_ascii=False,
                                                                                    sort_keys=True)},
                   {"type": "text", "text": "REQUEST_TEXT (untrusted data, verbatim):\n<<<\n"
                                            + request.request_text + "\n>>>"}]
        raw = self._client.complete_json(purpose="adjudication", version=PROMPT_VERSION, system=self._system,
                                         content=content, schema=OUTPUT_SCHEMA)
        if raw is None:
            return None
        try:
            answer = AdjudicationOut.model_validate(raw)
        except ValidationError as exc:
            log.warning("%s: adjudication output failed validation: %s", request.case_id, exc)
            return None
        return self.merge(request, views, answer)

    @staticmethod
    def merge(request: DeletionRequest, views: list[ResourceView], answer: AdjudicationOut) -> Review:
        notes: list[str] = []
        rules_intent = classify_intent(request.request_text)
        intent = IntentResult(answer.intent.is_deletion, answer.intent.request_type, "model")
        if intent.is_deletion and rules_intent.basis == "rules:explicit-non-deletion":
            intent = rules_intent  # an explicit "do not delete" in the text is a hard stop
            notes.append("model intent overridden by explicit non-deletion wording")
        by_ref = {record.ref: record for record in answer.records}
        kept: list[ResourceView] = []
        for index, view in enumerate(views, start=1):
            verdict = by_ref.get(f"R{index}")
            if verdict is None:
                kept.append(view)
                continue
            if verdict.belongs_to_subject == "no" and _soft(view):
                notes.append(f"dropped {view.resource_id}: {verdict.reason}")
                continue
            category = view.category
            stated = view.state is not None and view.state.category_stated
            default = DEFAULT_CATEGORY_BY_SYSTEM.get(view.system_id)
            if (not stated and view.category == default and verdict.category != view.category
                    and verdict.category in REFINABLE.get(view.system_id, set())):
                notes.append(f"{view.resource_id}: category {view.category.value if view.category else None} -> {verdict.category.value}")
                category = verdict.category
            kept.append(replace(view, category=category))
        return Review(intent, kept, answer.injection_detected, tuple(notes))
