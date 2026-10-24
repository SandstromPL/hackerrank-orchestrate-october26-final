"""Does the request text authorize deletion?

The language model is the primary classifier (see adjudication). This module is the
deterministic fallback used when no model is configured or its answer fails validation: an
explicit refusal of deletion wins over deletion vocabulary, so "I am not asking for deletion"
is never read as a deletion request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NEGATIONS = (
    r"not asking for (?:deletion|erasure)", r"not a request to (?:erase|delete)", r"\bdon'?t delete\b",
    r"\bdo not delete\b", r"\bdo not close\b", r"\bdon'?t close\b", r"leave (?:the|my) account active",
    r"keep (?:the|my) account", r"without removing", r"continue using the service", r"still need the account",
    r"still use it", r"leave my profile", r"\bonly\b.*marketing|marketing lists only",
)
DELETION_TERMS = (
    r"\bdelet", r"\berase", r"\berasure\b", r"\bremov", r"close (?:my|this|the) account", r"\bclosed my account\b",
    r"\b(?:is|are) gone\b", r"suppression", r"supprim", r"eliminaci", r"eliminar", r"exclus[aã]o", r"excluir",
    r"削除", r"삭제",
)
OTHER_REQUESTS = {
    "MARKETING_OPT_OUT": r"promotional|marketing",
    "DATA_EXPORT": r"download|portable|copy of|send me a copy",
    "ACCESS": r"what personal information|information request",
    "CORRECTION": r"misspelled|correct it|out of date|replace it|change the email",
    "ACCOUNT_SUPPORT": r"reset(?:ting)? the login|can'?t access",
    "BILLING": r"invoice",
}


@dataclass(frozen=True)
class IntentResult:
    is_deletion: bool
    request_type: str
    basis: str  # which rule or model produced the answer


def classify_intent(text: str) -> IntentResult:
    lowered = text.lower()
    if any(re.search(pattern, lowered) for pattern in NEGATIONS):
        kind = next((name for name, pattern in OTHER_REQUESTS.items() if re.search(pattern, lowered)), "OTHER")
        return IntentResult(False, kind, "rules:explicit-non-deletion")
    if any(re.search(pattern, lowered) for pattern in DELETION_TERMS):
        return IntentResult(True, "DELETION", "rules:deletion-term")
    kind = next((name for name, pattern in OTHER_REQUESTS.items() if re.search(pattern, lowered)), "OTHER")
    return IntentResult(False, kind, "rules:no-deletion-term")
