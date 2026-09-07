"""Tamper-evident records for discrepancy resolution decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

GENESIS_DIGEST = "0" * 64


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("evidence timestamps must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical(payload: Mapping[str, str]) -> str:
    return json.dumps(dict(sorted(payload.items())), ensure_ascii=False, separators=(",", ":"))


class VerificationCode(StrEnum):
    DUPLICATE_ID = "duplicate-id"
    SUBJECT_MISMATCH = "subject-mismatch"
    CHRONOLOGY_ERROR = "chronology-error"
    PREVIOUS_DIGEST_MISMATCH = "previous-digest-mismatch"
    DIGEST_MISMATCH = "digest-mismatch"
    MISSING_KIND = "missing-kind"


@dataclass(frozen=True, slots=True)
class EvidenceDraft:
    evidence_id: str
    kind: str
    occurred_at: datetime
    actor: str
    attributes: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("evidence id must not be blank")
        if not self.kind.strip():
            raise ValueError("evidence kind must not be blank")
        if not self.actor.strip():
            raise ValueError("evidence actor must not be blank")
        _timestamp(self.occurred_at)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    subject_id: str
    kind: str
    occurred_at: datetime
    actor: str
    attributes: Mapping[str, str]
    previous_digest: str
    digest: str

    def canonical_payload(self) -> str:
        body = {
            "actor": self.actor,
            "attributes": _canonical(self.attributes),
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "occurred_at": _timestamp(self.occurred_at),
            "previous_digest": self.previous_digest,
            "subject_id": self.subject_id,
        }
        return _canonical(body)

    def calculated_digest(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    code: VerificationCode
    evidence_id: str | None
    explanation: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    subject_id: str
    record_count: int
    kinds: frozenset[str]
    terminal_digest: str
    issues: tuple[VerificationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues


class DiscrepancyResolutionEvidenceChain:
    """Build and verify an append-only evidence chain for one subject."""

    def __init__(self, subject_id: str, records: Sequence[EvidenceRecord] = ()) -> None:
        if not subject_id.strip():
            raise ValueError("subject id must not be blank")
        self._subject_id = subject_id
        self._records = list(records)

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records)

    @property
    def terminal_digest(self) -> str:
        if not self._records:
            return GENESIS_DIGEST
        return self._records[-1].digest

    def append(self, draft: EvidenceDraft) -> EvidenceRecord:
        if any(record.evidence_id == draft.evidence_id for record in self._records):
            raise ValueError(f"duplicate evidence id: {draft.evidence_id}")
        if self._records and draft.occurred_at < self._records[-1].occurred_at:
            raise ValueError("evidence chronology must not move backwards")
        record = EvidenceRecord(
            evidence_id=draft.evidence_id,
            subject_id=self._subject_id,
            kind=draft.kind,
            occurred_at=draft.occurred_at,
            actor=draft.actor,
            attributes=dict(draft.attributes),
            previous_digest=self.terminal_digest,
            digest="",
        )
        sealed = EvidenceRecord(
            evidence_id=record.evidence_id,
            subject_id=record.subject_id,
            kind=record.kind,
            occurred_at=record.occurred_at,
            actor=record.actor,
            attributes=record.attributes,
            previous_digest=record.previous_digest,
            digest=record.calculated_digest(),
        )
        self._records.append(sealed)
        return sealed

    def verify(self, required_kinds: Iterable[str] = ()) -> VerificationReport:
        issues: list[VerificationIssue] = []
        seen: set[str] = set()
        previous = GENESIS_DIGEST
        previous_time: datetime | None = None
        kinds: set[str] = set()

        for record in self._records:
            kinds.add(record.kind)
            if record.evidence_id in seen:
                issues.append(
                    VerificationIssue(
                        VerificationCode.DUPLICATE_ID,
                        record.evidence_id,
                        "evidence id occurs more than once",
                    )
                )
            seen.add(record.evidence_id)
            if record.subject_id != self._subject_id:
                issues.append(
                    VerificationIssue(
                        VerificationCode.SUBJECT_MISMATCH,
                        record.evidence_id,
                        "record belongs to another subject",
                    )
                )
            if previous_time is not None and record.occurred_at < previous_time:
                issues.append(
                    VerificationIssue(
                        VerificationCode.CHRONOLOGY_ERROR,
                        record.evidence_id,
                        "record chronology moved backwards",
                    )
                )
            if record.previous_digest != previous:
                issues.append(
                    VerificationIssue(
                        VerificationCode.PREVIOUS_DIGEST_MISMATCH,
                        record.evidence_id,
                        "record does not reference the preceding digest",
                    )
                )
            if record.digest != record.calculated_digest():
                issues.append(
                    VerificationIssue(
                        VerificationCode.DIGEST_MISMATCH,
                        record.evidence_id,
                        "record digest does not match its canonical payload",
                    )
                )
            previous = record.digest
            previous_time = record.occurred_at

        for kind in sorted(set(required_kinds).difference(kinds)):
            issues.append(
                VerificationIssue(
                    VerificationCode.MISSING_KIND,
                    None,
                    f"required evidence kind is missing: {kind}",
                )
            )
        return VerificationReport(
            subject_id=self._subject_id,
            record_count=len(self._records),
            kinds=frozenset(kinds),
            terminal_digest=previous,
            issues=tuple(issues),
        )


def default_required_kinds() -> tuple[str, ...]:
    return ("input-profile", "schema-contract", "result-delta")


def evidence_projection(records: Iterable[EvidenceRecord]) -> tuple[dict[str, str], ...]:
    """Return a stable summary safe for line-oriented audit export."""

    return tuple(
        {
            "actor": record.actor,
            "digest": record.digest,
            "evidence_id": record.evidence_id,
            "kind": record.kind,
            "occurred_at": _timestamp(record.occurred_at),
            "subject_id": record.subject_id,
        }
        for record in records
    )
