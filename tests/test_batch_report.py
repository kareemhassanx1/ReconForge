from __future__ import annotations

from pathlib import Path

import pytest

from reconforge.batch import BatchPair, BatchPlan, reconcile_batch
from reconforge.batch_report import (
    BatchExecution,
    BatchReport,
    BatchReportPair,
    batch_report_from_result,
    batch_report_to_dict,
)
from reconforge.reconcile import ReconciliationConfig
from reconforge.report import batch_result_to_dict


def write_file(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_batch(tmp_path: Path):
    left = write_file(tmp_path, "left.csv", "id,value\n1,A\n")
    right = write_file(tmp_path, "right.csv", "id,value\n1,B\n")
    unmatched = write_file(tmp_path, "left-only.csv", "id,value\n2,C\n")
    result = reconcile_batch(
        BatchPlan((BatchPair("orders", left, right),), unmatched_left=(unmatched,)),
        ReconciliationConfig(key_fields=("id",)),
    )
    return result


def test_batch_report_model_round_trips_fresh_result_without_format_drift(tmp_path: Path) -> None:
    result = make_batch(tmp_path)
    execution = {
        "checkpoint": str(tmp_path / "checkpoint.json"),
        "executed_pairs": ["orders"],
        "reused_pairs": [],
    }

    model = batch_report_from_result(result, execution=execution)

    assert batch_report_to_dict(model) == batch_result_to_dict(result, execution=execution)
    assert model.issue_count == 2
    assert not model.is_clean


def test_batch_report_model_canonicalizes_pair_and_unmatched_order(tmp_path: Path) -> None:
    result = make_batch(tmp_path)
    pair = batch_report_from_result(result).pairs[0]

    model = BatchReport(
        pairs=(
            BatchReportPair("zeta", "z-left", "z-right", pair.result),
            BatchReportPair("Alpha", "a-left", "a-right", pair.result),
        ),
        unmatched_left=("z.csv", "A.csv"),
    )

    assert [item.name for item in model.pairs] == ["Alpha", "zeta"]
    assert model.unmatched_left == ("A.csv", "z.csv")


def test_batch_execution_requires_disjoint_complete_pair_coverage(tmp_path: Path) -> None:
    pair = batch_report_from_result(make_batch(tmp_path)).pairs[0]

    with pytest.raises(ValueError, match="both executed and reused"):
        BatchExecution("checkpoint.json", ("orders",), ("orders",))
    with pytest.raises(ValueError, match="cover every report pair.*missing"):
        BatchReport((pair,), execution=BatchExecution("checkpoint.json", (), ()))
