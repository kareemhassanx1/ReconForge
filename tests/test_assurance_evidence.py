from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from reconforge.assurance.evidence import (
    GENESIS_DIGEST,
    DiscrepancyResolutionEvidenceChain,
    EvidenceDraft,
    VerificationCode,
    default_required_kinds,
    evidence_projection,
)


class EvidenceChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 7, 12, tzinfo=UTC)

    def _draft(self, identifier: str, kind: str, offset: int = 0) -> EvidenceDraft:
        return EvidenceDraft(
            identifier,
            kind,
            self.now + timedelta(minutes=offset),
            "operator-1",
            {"source": "test", "sequence": str(offset)},
        )

    def test_builds_and_verifies_complete_chain(self) -> None:
        chain = DiscrepancyResolutionEvidenceChain("subject-1")
        for index, kind in enumerate(default_required_kinds()):
            chain.append(self._draft(f"ev-{index}", kind, index))
        report = chain.verify(default_required_kinds())
        self.assertTrue(report.valid)
        self.assertEqual(report.record_count, 3)
        self.assertNotEqual(report.terminal_digest, GENESIS_DIGEST)
        self.assertEqual(chain.records[0].previous_digest, GENESIS_DIGEST)
        self.assertEqual(chain.records[1].previous_digest, chain.records[0].digest)
        projection = evidence_projection(chain.records)
        self.assertEqual(projection[0]["subject_id"], "subject-1")
        self.assertEqual(projection[-1]["digest"], report.terminal_digest)

    def test_reports_missing_evidence_kind(self) -> None:
        chain = DiscrepancyResolutionEvidenceChain("subject-2")
        chain.append(self._draft("ev-1", default_required_kinds()[0]))
        report = chain.verify(default_required_kinds())
        self.assertFalse(report.valid)
        self.assertEqual(
            [issue.code for issue in report.issues],
            [VerificationCode.MISSING_KIND, VerificationCode.MISSING_KIND],
        )

    def test_detects_tampering_and_broken_links(self) -> None:
        original = DiscrepancyResolutionEvidenceChain("subject-3")
        first = original.append(self._draft("ev-1", "first"))
        second = original.append(self._draft("ev-2", "second", 1))
        corrupted = replace(second, previous_digest="f" * 64, digest="bad")
        duplicate = replace(first, subject_id="other", occurred_at=self.now - timedelta(minutes=1))
        chain = DiscrepancyResolutionEvidenceChain("subject-3", (first, corrupted, duplicate))
        report = chain.verify()
        codes = {issue.code for issue in report.issues}
        self.assertIn(VerificationCode.PREVIOUS_DIGEST_MISMATCH, codes)
        self.assertIn(VerificationCode.DIGEST_MISMATCH, codes)
        self.assertIn(VerificationCode.DUPLICATE_ID, codes)
        self.assertIn(VerificationCode.SUBJECT_MISMATCH, codes)
        self.assertIn(VerificationCode.CHRONOLOGY_ERROR, codes)

    def test_rejects_invalid_drafts_and_append_order(self) -> None:
        with self.assertRaises(ValueError):
            DiscrepancyResolutionEvidenceChain("")
        with self.assertRaises(ValueError):
            EvidenceDraft("", "kind", self.now, "actor", {})
        with self.assertRaises(ValueError):
            EvidenceDraft("id", "", self.now, "actor", {})
        with self.assertRaises(ValueError):
            EvidenceDraft("id", "kind", self.now, "", {})
        with self.assertRaises(ValueError):
            EvidenceDraft("id", "kind", datetime(2026, 1, 1), "actor", {})

        chain = DiscrepancyResolutionEvidenceChain("subject-4")
        chain.append(self._draft("later", "kind", 10))
        with self.assertRaises(ValueError):
            chain.append(self._draft("earlier", "kind", 0))
        with self.assertRaises(ValueError):
            chain.append(self._draft("later", "kind", 11))


if __name__ == "__main__":
    unittest.main()
