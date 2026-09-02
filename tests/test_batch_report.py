from __future__ import annotations

import json
from pathlib import Path

import pytest

from reconforge.batch import BatchPair, BatchPlan, reconcile_batch
from reconforge.batch_report import (
    BatchExecution,
    BatchReport,
    BatchReportPair,
    batch_report_from_dict,
    batch_report_from_result,
    batch_report_to_dict,
    load_json_batch_report,
)
from reconforge.errors import InputError
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


def test_batch_report_parser_round_trips_generated_report(tmp_path: Path) -> None:
    result = make_batch(tmp_path)
    payload = batch_result_to_dict(
        result,
        execution={
            "checkpoint": str(tmp_path / "checkpoint.json"),
            "executed_pairs": ["orders"],
            "reused_pairs": [],
        },
    )

    parsed = batch_report_from_dict(payload)

    assert batch_report_to_dict(parsed) == payload


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda report: report.update(batch_report_version=2), "unsupported batch report version"),
        (lambda report: report.update(extra=True), "contains unknown field.*extra"),
        (lambda report: report.pop("pairs"), "missing required field.*pairs"),
        (
            lambda report: report["pairs"][0]["result"].update(status="clean"),
            r"pairs\[0\]\.result.*report.status is 'clean'",
        ),
    ],
)
def test_batch_report_parser_rejects_invalid_envelope_and_nested_result(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = batch_result_to_dict(make_batch(tmp_path))
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        batch_report_from_dict(payload)


def test_batch_report_loader_accepts_bom_and_object_key_reordering(tmp_path: Path) -> None:
    payload = batch_result_to_dict(make_batch(tmp_path))
    reordered = dict(reversed(tuple(payload.items())))
    source = tmp_path / "batch-report.json"
    source.write_text(json.dumps(reordered, ensure_ascii=False), encoding="utf-8-sig")

    loaded = load_json_batch_report(source)

    assert batch_report_to_dict(loaded) == payload


def test_batch_report_loader_rejects_duplicate_keys_at_any_depth(tmp_path: Path) -> None:
    payload = batch_result_to_dict(make_batch(tmp_path))
    text = json.dumps(payload).replace(
        '"status": "issues_found"',
        '"status": "issues_found", "status": "issues_found"',
        1,
    )
    source = tmp_path / "duplicate.json"
    source.write_text(text, encoding="utf-8")

    with pytest.raises(InputError, match="duplicate key 'status'"):
        load_json_batch_report(source)


def test_batch_report_loader_reports_file_and_json_diagnostics(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="does not exist"):
        load_json_batch_report(tmp_path / "missing.json")

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"batch_report_version":', encoding="utf-8")
    with pytest.raises(InputError, match=r"line 1, column \d+"):
        load_json_batch_report(malformed)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("issues", 99, r"summary\.issues is 99; expected 2"),
        ("paired_inputs", True, "paired_inputs must be a non-negative integer"),
        ("unmatched_left", -1, "unmatched_left must be a non-negative integer"),
    ],
)
def test_batch_report_parser_recomputes_aggregate_summary(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    payload = batch_result_to_dict(make_batch(tmp_path))
    payload["summary"][field] = value

    with pytest.raises(ValueError, match=message):
        batch_report_from_dict(payload)


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
