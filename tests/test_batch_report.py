from __future__ import annotations

import json
from pathlib import Path

import pytest

import reconforge
from reconforge.batch import BatchPair, BatchPlan, reconcile_batch
from reconforge.batch_report import (
    BatchExecution,
    BatchReport,
    BatchReportPair,
    batch_pair_fingerprints,
    batch_report_fingerprint,
    batch_report_from_dict,
    batch_report_from_result,
    batch_report_to_dict,
    load_json_batch_report,
    load_saved_report,
)
from reconforge.cli import main
from reconforge.errors import InputError
from reconforge.model import ReconciliationResult
from reconforge.reconcile import ReconciliationConfig
from reconforge.report import batch_result_to_dict, result_fingerprint, result_to_dict


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


def test_batch_report_parser_rejects_boolean_nested_report_version(tmp_path: Path) -> None:
    payload = batch_result_to_dict(make_batch(tmp_path))
    payload["pairs"][0]["result"]["report_version"] = True

    with pytest.raises(ValueError, match=r"pairs\[0\]\.result.*unsupported report version True"):
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


def test_batch_report_parser_derives_status_from_complete_outcome(tmp_path: Path) -> None:
    payload = batch_result_to_dict(make_batch(tmp_path))
    payload["status"] = "clean"

    with pytest.raises(ValueError, match="status is 'clean'; expected 'issues_found'"):
        batch_report_from_dict(payload)


@pytest.mark.parametrize(
    ("location", "mutate"),
    [
        ("status", lambda report: report.update(status=" issues_found ")),
        ("pairs\\[0\\]\\.name", lambda report: report["pairs"][0].update(name=" orders ")),
        (
            "execution executed_pairs item",
            lambda report: report["execution"].update(executed_pairs=[" orders "]),
        ),
    ],
)
def test_batch_report_parser_rejects_noncanonical_semantic_whitespace(
    tmp_path: Path, location: str, mutate
) -> None:
    payload = batch_result_to_dict(
        make_batch(tmp_path),
        execution={
            "checkpoint": str(tmp_path / "checkpoint.json"),
            "executed_pairs": ["orders"],
            "reused_pairs": [],
        },
    )
    mutate(payload)

    with pytest.raises(ValueError, match=rf"{location}.*leading or trailing whitespace"):
        batch_report_from_dict(payload)


def test_cli_verify_returns_two_for_noncanonical_batch_status(tmp_path: Path, capsys) -> None:
    payload = batch_result_to_dict(make_batch(tmp_path))
    payload["status"] = " issues_found "
    source = tmp_path / "batch.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["verify", str(source)]) == 2
    assert "leading or trailing whitespace" in capsys.readouterr().err


def test_cli_verify_returns_two_for_excessively_nested_json(tmp_path: Path, capsys) -> None:
    source = tmp_path / "deep.json"
    source.write_text(
        '{"report_version":1,"extra":' + "[" * 5000 + "0" + "]" * 5000 + "}",
        encoding="utf-8",
    )

    assert main(["verify", str(source)]) == 2
    assert "exceeds the maximum nesting depth" in capsys.readouterr().err


def test_batch_report_fingerprint_is_semantic_and_ignores_execution_provenance(
    tmp_path: Path,
) -> None:
    result = make_batch(tmp_path)
    executed = batch_report_from_result(
        result,
        execution={
            "checkpoint": str(tmp_path / "first.json"),
            "executed_pairs": ["orders"],
            "reused_pairs": [],
        },
    )
    reused = batch_report_from_result(
        result,
        execution={
            "checkpoint": str(tmp_path / "second.json"),
            "executed_pairs": [],
            "reused_pairs": ["orders"],
        },
    )

    assert batch_report_fingerprint(executed) == batch_report_fingerprint(reused)
    assert batch_report_fingerprint(executed).startswith("sha256:")
    assert len(batch_report_fingerprint(executed)) == len("sha256:") + 64


def test_batch_report_fingerprint_changes_with_reconciliation_content(tmp_path: Path) -> None:
    report = batch_report_from_result(make_batch(tmp_path))
    changed = BatchReport(report.pairs, report.unmatched_left + ("another.csv",))

    assert batch_report_fingerprint(report) != batch_report_fingerprint(changed)


def test_batch_report_fingerprint_validates_public_input() -> None:
    with pytest.raises(TypeError, match="must be a BatchReport"):
        batch_report_fingerprint(object())  # type: ignore[arg-type]


def test_batch_pair_fingerprints_identify_each_nested_result(tmp_path: Path) -> None:
    report = batch_report_from_result(make_batch(tmp_path))

    fingerprints = batch_pair_fingerprints(report)

    assert fingerprints == (("orders", result_fingerprint(report.pairs[0].result)),)
    with pytest.raises(TypeError, match="must be a BatchReport"):
        batch_pair_fingerprints(object())  # type: ignore[arg-type]


def test_saved_report_loader_auto_detects_single_and_batch_reports(tmp_path: Path) -> None:
    batch = batch_report_from_result(make_batch(tmp_path))
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(batch_report_to_dict(batch)), encoding="utf-8")
    single_path = tmp_path / "single.json"
    single_path.write_text(json.dumps(result_to_dict(batch.pairs[0].result)), encoding="utf-8")

    assert isinstance(load_saved_report(batch_path), BatchReport)
    assert isinstance(load_saved_report(single_path), ReconciliationResult)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"report_version": 1, "batch_report_version": 1},
            "contains both report_version and batch_report_version",
        ),
        ({"version": 1}, "must contain either report_version or batch_report_version"),
    ],
)
def test_saved_report_loader_rejects_ambiguous_or_untyped_documents(
    tmp_path: Path, payload: object, message: str
) -> None:
    source = tmp_path / "report.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputError, match=message):
        load_saved_report(source)


def test_cli_verify_supports_strict_batch_reports(tmp_path: Path, capsys) -> None:
    source = tmp_path / "batch.json"
    source.write_text(
        json.dumps(batch_result_to_dict(make_batch(tmp_path)), ensure_ascii=False),
        encoding="utf-8",
    )

    status = main(["verify", str(source)])

    verified = json.loads(capsys.readouterr().out)
    assert status == 0
    assert verified["valid"] is True
    assert verified["report_type"] == "batch"
    assert verified["batch_report_version"] == 1
    assert verified["status"] == "issues_found"
    assert verified["summary"]["issues"] == 2
    assert verified["fingerprint"].startswith("sha256:")
    assert verified["pair_fingerprints"] == [
        {
            "name": "orders",
            "fingerprint": result_fingerprint(
                batch_report_from_result(make_batch(tmp_path)).pairs[0].result
            ),
        }
    ]


def test_cli_verify_checks_expected_batch_fingerprint(tmp_path: Path, capsys) -> None:
    source = tmp_path / "batch.json"
    source.write_text(json.dumps(batch_result_to_dict(make_batch(tmp_path))), encoding="utf-8")
    assert main(["verify", str(source)]) == 0
    fingerprint = json.loads(capsys.readouterr().out)["fingerprint"]

    assert main(["verify", str(source), "--expect-fingerprint", fingerprint]) == 0
    assert json.loads(capsys.readouterr().out)["matches_expected"] is True

    different = "sha256:" + "0" * 64
    assert main(["verify", str(source), "--expect-fingerprint", different]) == 1
    assert json.loads(capsys.readouterr().out)["matches_expected"] is False


def test_batch_report_verification_api_is_public() -> None:
    assert reconforge.BATCH_REPORT_VERSION == 1
    assert reconforge.BatchReport is BatchReport
    assert reconforge.batch_report_from_dict is batch_report_from_dict
    assert reconforge.batch_report_fingerprint is batch_report_fingerprint
    assert reconforge.load_json_batch_report is load_json_batch_report
    assert reconforge.load_saved_report is load_saved_report


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
