"""Compare predicted rows with expected rows.

Metrics
  * status accuracy: deletion_status matches;
  * step precision/recall/F1 on (phase, system_id, resource_id, operation, state), ignoring
    evidence, and again with evidence_id included;
  * field accuracy: steps aligned on (phase, system_id, resource_id), then each field compared;
  * sequence accuracy: the whole ordered step list is identical (all 7 fields);
  * error buckets per failing case, so the next fix targets the biggest bucket.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

CORE = ("phase", "system_id", "resource_id", "operation", "state")


def _core(step: dict) -> tuple:
    return tuple(step[k] for k in CORE)


def _full(step: dict) -> tuple:
    return _core(step) + (step["evidence_id"],)


def _canonical(steps: list[dict]) -> list[tuple]:
    return [(s["step_no"],) + _full(s) for s in steps]


@dataclass
class CaseScore:
    case_id: str
    expected_status: str
    predicted_status: str
    expected: list[dict]
    predicted: list[dict]
    buckets: list[str] = field(default_factory=list)

    @property
    def status_ok(self) -> bool:
        return self.expected_status == self.predicted_status

    @property
    def sequence_ok(self) -> bool:
        return self.status_ok and _canonical(self.expected) == _canonical(self.predicted)

    def matched(self, key=_core) -> int:
        return sum((Counter(map(key, self.expected)) & Counter(map(key, self.predicted))).values())


def score_case(case_id: str, expected: tuple[str, str], predicted: tuple[str, str]) -> CaseScore:
    score = CaseScore(case_id, expected[0], predicted[0], json.loads(expected[1]), json.loads(predicted[1]))
    exp_core, pred_core = Counter(map(_core, score.expected)), Counter(map(_core, score.predicted))
    if not score.status_ok:
        score.buckets.append(f"status:{score.expected_status}->{score.predicted_status}")
    for key in (exp_core - pred_core):
        score.buckets.append(f"missing:{key[0]}:{key[3]}:{key[1]}")
    for key in (pred_core - exp_core):
        score.buckets.append(f"extra:{key[0]}:{key[3]}:{key[1]}")
    exp_full = {_core(s): s["evidence_id"] for s in score.expected}
    for step in score.predicted:
        if _core(step) in exp_full and exp_full[_core(step)] != step["evidence_id"]:
            score.buckets.append(f"evidence:{step['phase']}:{step['system_id']}")
    if not score.buckets and not score.sequence_ok:
        score.buckets.append("order")
    return score


@dataclass
class Report:
    cases: list[CaseScore]

    def metrics(self) -> dict[str, float]:
        n = len(self.cases) or 1
        expected = sum(len(c.expected) for c in self.cases)
        predicted = sum(len(c.predicted) for c in self.cases)

        def prf(key) -> tuple[float, float, float]:
            hit = sum(c.matched(key) for c in self.cases)
            p = hit / predicted if predicted else 1.0
            r = hit / expected if expected else 1.0
            return p, r, (2 * p * r / (p + r) if p + r else 0.0)

        p, r, f = prf(_core)
        pe, re_, fe = prf(_full)
        aligned = Counter()
        field_hits = Counter()
        resource_hits = resource_total = 0
        for case in self.cases:
            by_slot = {(s["phase"], s["system_id"], s["resource_id"]): s for s in case.predicted}
            for step in case.expected:
                slot = (step["phase"], step["system_id"], step["resource_id"])
                if slot in by_slot:
                    aligned["n"] += 1
                    for name in ("operation", "state", "evidence_id"):
                        field_hits[name] += by_slot[slot][name] == step[name]
            exp_res = {(s["system_id"], s["resource_id"]) for s in case.expected}
            pred_res = {(s["system_id"], s["resource_id"]) for s in case.predicted}
            resource_hits += len(exp_res & pred_res)
            resource_total += len(exp_res)
        return {
            "cases": len(self.cases),
            "status_accuracy": sum(c.status_ok for c in self.cases) / n,
            "step_precision": p, "step_recall": r, "step_f1": f,
            "step_f1_with_evidence": fe, "step_precision_with_evidence": pe, "step_recall_with_evidence": re_,
            "resource_recall": resource_hits / resource_total if resource_total else 1.0,
            "field_operation_acc": field_hits["operation"] / aligned["n"] if aligned["n"] else 0.0,
            "field_state_acc": field_hits["state"] / aligned["n"] if aligned["n"] else 0.0,
            "field_evidence_acc": field_hits["evidence_id"] / aligned["n"] if aligned["n"] else 0.0,
            "sequence_accuracy": sum(c.sequence_ok for c in self.cases) / n,
            "expected_steps": expected, "predicted_steps": predicted,
        }

    def buckets(self) -> Counter:
        counter: Counter = Counter()
        for case in self.cases:
            for bucket in case.buckets:
                counter[bucket.split(":")[0] + ":" + ":".join(bucket.split(":")[1:3])] += 1
        return counter
