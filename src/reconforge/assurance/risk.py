"""Deterministic risk controls for a reconciliation batch.

The module turns measured operational signals into an explainable decision.
It deliberately separates measurement, policy and presentation so callers can
persist every input and reproduce a decision without hidden global state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class Direction(StrEnum):
    """How a measured value is compared with its policy boundary."""

    MAXIMUM = "maximum"
    MINIMUM = "minimum"


class Severity(StrEnum):
    """Operational impact assigned to a failed control."""

    ADVISORY = "advisory"
    MATERIAL = "material"
    BLOCKING = "blocking"


class Disposition(StrEnum):
    """Result of the complete reconciliation batch assessment."""

    ACCEPT = "accept"
    REVIEW = "review"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class ControlRule:
    """One named, weighted and explainable policy boundary."""

    code: str
    label: str
    boundary: float
    direction: Direction
    severity: Severity
    weight: int = 1

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("control code must not be blank")
        if not self.label.strip():
            raise ValueError("control label must not be blank")
        if self.weight < 1:
            raise ValueError("control weight must be positive")


@dataclass(frozen=True, slots=True)
class Signal:
    """A measured value with the evidence references that produced it."""

    control_code: str
    measured: float
    evidence_refs: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if not self.control_code.strip():
            raise ValueError("signal control code must not be blank")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("signal evidence references must be unique")


@dataclass(frozen=True, slots=True)
class Finding:
    """Auditable result of evaluating one signal against one rule."""

    control_code: str
    label: str
    passed: bool
    measured: float
    boundary: float
    direction: Direction
    severity: Severity
    weight: int
    evidence_refs: tuple[str, ...]
    explanation: str


@dataclass(frozen=True, slots=True)
class Assessment:
    """Complete decision and diagnostics for a single reconciliation batch."""

    subject_id: str
    disposition: Disposition
    risk_points: int
    maximum_points: int
    findings: tuple[Finding, ...]
    missing_controls: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.disposition is Disposition.ACCEPT

    @property
    def failed_findings(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if not finding.passed)

    @property
    def risk_ratio(self) -> float:
        if self.maximum_points == 0:
            return 0.0
        return self.risk_points / self.maximum_points


def _passes(rule: ControlRule, measured: float) -> bool:
    if rule.direction is Direction.MAXIMUM:
        return measured <= rule.boundary
    return measured >= rule.boundary


def _explain(rule: ControlRule, measured: float, passed: bool) -> str:
    comparator = "at most" if rule.direction is Direction.MAXIMUM else "at least"
    outcome = "passes" if passed else "fails"
    return (
        f"{rule.label} {outcome}: observed {measured:g}; "
        f"policy requires {comparator} {rule.boundary:g}."
    )


class ReconciliationBatchRiskEngine:
    """Evaluate reconciliation batch signals using an immutable policy set."""

    def __init__(self, rules: Sequence[ControlRule]) -> None:
        if not rules:
            raise ValueError("at least one control rule is required")
        indexed = {rule.code: rule for rule in rules}
        if len(indexed) != len(rules):
            raise ValueError("control rule codes must be unique")
        self._rules: Mapping[str, ControlRule] = indexed

    @property
    def rule_codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def assess(self, subject_id: str, signals: Iterable[Signal]) -> Assessment:
        if not subject_id.strip():
            raise ValueError("subject id must not be blank")
        materialized = tuple(signals)
        by_code = {signal.control_code: signal for signal in materialized}
        if len(by_code) != len(materialized):
            raise ValueError("a control may have only one signal per assessment")
        unknown = sorted(set(by_code).difference(self._rules))
        if unknown:
            raise ValueError(f"unknown controls: {', '.join(unknown)}")

        findings: list[Finding] = []
        points = 0
        maximum = sum(rule.weight for rule in self._rules.values())
        for code in sorted(by_code):
            signal = by_code[code]
            rule = self._rules[code]
            passed = _passes(rule, signal.measured)
            if not passed:
                points += rule.weight
            findings.append(
                Finding(
                    control_code=code,
                    label=rule.label,
                    passed=passed,
                    measured=signal.measured,
                    boundary=rule.boundary,
                    direction=rule.direction,
                    severity=rule.severity,
                    weight=rule.weight,
                    evidence_refs=signal.evidence_refs,
                    explanation=_explain(rule, signal.measured, passed),
                )
            )

        missing = tuple(sorted(set(self._rules).difference(by_code)))
        disposition = self._disposition(findings, missing, points, maximum)
        return Assessment(
            subject_id=subject_id,
            disposition=disposition,
            risk_points=points,
            maximum_points=maximum,
            findings=tuple(findings),
            missing_controls=missing,
        )

    def assess_many(
        self,
        assessments: Mapping[str, Iterable[Signal]],
    ) -> tuple[Assessment, ...]:
        return tuple(
            self.assess(subject_id, assessments[subject_id]) for subject_id in sorted(assessments)
        )

    @staticmethod
    def _disposition(
        findings: Sequence[Finding],
        missing: Sequence[str],
        points: int,
        maximum: int,
    ) -> Disposition:
        if missing:
            return Disposition.HOLD
        failures = tuple(finding for finding in findings if not finding.passed)
        if any(finding.severity is Severity.BLOCKING for finding in failures):
            return Disposition.HOLD
        if not failures:
            return Disposition.ACCEPT
        ratio = points / maximum if maximum else 0.0
        if ratio >= 0.5:
            return Disposition.HOLD
        return Disposition.REVIEW


def summarize_assessments(assessments: Iterable[Assessment]) -> dict[str, int]:
    """Count dispositions without discarding individual assessment evidence."""

    summary = {disposition.value: 0 for disposition in Disposition}
    for assessment in assessments:
        summary[assessment.disposition.value] += 1
    return summary


def prioritize_reviews(assessments: Iterable[Assessment]) -> tuple[Assessment, ...]:
    """Order non-accepted work by impact, incompleteness and stable identity."""

    pending = [item for item in assessments if not item.passed]
    return tuple(
        sorted(
            pending,
            key=lambda item: (
                item.disposition is not Disposition.HOLD,
                -len(item.missing_controls),
                -item.risk_points,
                item.subject_id,
            ),
        )
    )
