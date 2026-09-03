from __future__ import annotations

import json
from pathlib import Path

import pytest

import reconforge.cli as cli_module
from reconforge.batch import (
    BatchError,
    BatchPair,
    BatchPairResult,
    BatchPlan,
    BatchResult,
    discover_directory_batch,
    load_batch_manifest,
    reconcile_batch,
)
from reconforge.cli import main
from reconforge.json_safety import MAX_JSON_NESTING_DEPTH
from reconforge.reconcile import ReconciliationConfig
from reconforge.report import batch_result_to_dict, write_json_batch_report


def write_file(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_directory_discovery_pairs_recursively_in_stable_order(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    left_b = write_file(left, "B.csv", "id,value\n1,same\n")
    left_a = write_file(left, "nested/a.csv", "id,value\n1,same\n")
    right_a = write_file(right, "nested/a.json", '[{"id": 1, "value": "same"}]')
    right_b = write_file(right, "B.ndjson", '{"id": 1, "value": "same"}\n')

    plan = discover_directory_batch(left, right)

    assert [(pair.name, pair.left, pair.right) for pair in plan.pairs] == [
        ("B", left_b.resolve(), right_b.resolve()),
        ("nested/a", left_a.resolve(), right_a.resolve()),
    ]
    assert plan.unmatched_left == ()
    assert plan.unmatched_right == ()


def test_batch_result_rejects_duplicate_pair_names_case_insensitively(tmp_path: Path) -> None:
    left = write_file(tmp_path, "left.csv", "id\n1\n")
    right = write_file(tmp_path, "right.csv", "id\n1\n")
    pair_result = reconcile_batch(
        BatchPlan((BatchPair("orders", left, right),)),
        ReconciliationConfig(key_fields=("id",)),
    ).pairs[0]

    with pytest.raises(ValueError, match="result pair names must be unique"):
        BatchResult(
            (
                pair_result,
                BatchPairResult(BatchPair("ORDERS", left, right), pair_result.result),
            )
        )


def test_batch_models_reject_invalid_pair_members() -> None:
    with pytest.raises(TypeError, match="plan pairs must be BatchPair"):
        BatchPlan((object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="result pairs must be BatchPairResult"):
        BatchResult((object(),))  # type: ignore[arg-type]


def test_directory_discovery_reports_unmatched_files_explicitly(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    write_file(left, "paired.csv", "id\n1\n")
    write_file(right, "paired.csv", "id\n1\n")
    left_only = write_file(left, "left-only.json", "[]")
    right_only = write_file(right, "right-only.ndjson", "")
    write_file(left, "notes.txt", "ignored")

    plan = discover_directory_batch(left, right)

    assert [pair.name for pair in plan.pairs] == ["paired"]
    assert plan.unmatched_left == (left_only.resolve(),)
    assert plan.unmatched_right == (right_only.resolve(),)


def test_directory_discovery_rejects_ambiguous_same_side_identity(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    write_file(left, "orders.csv", "id\n1\n")
    write_file(left, "orders.json", '[{"id": 1}]')
    write_file(right, "orders.csv", "id\n1\n")

    with pytest.raises(BatchError, match=r"ambiguous left.*orders\.csv.*orders\.json"):
        discover_directory_batch(left, right)


def test_directory_discovery_rejects_two_empty_trees(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    with pytest.raises(BatchError, match="contain no recognized datasets"):
        discover_directory_batch(left, right)


def test_manifest_loads_named_pairs_relative_to_it_and_sorts_names(tmp_path: Path) -> None:
    left_a = write_file(tmp_path, "inputs/a-left.csv", "id\n1\n")
    right_a = write_file(tmp_path, "inputs/a-right.csv", "id\n1\n")
    left_z = write_file(tmp_path, "inputs/z-left.csv", "id\n1\n")
    right_z = write_file(tmp_path, "inputs/z-right.csv", "id\n1\n")
    manifest = tmp_path / "batch.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "pairs": [
                    {"name": "zeta", "left": "inputs/z-left.csv", "right": "inputs/z-right.csv"},
                    {"name": "alpha", "left": "inputs/a-left.csv", "right": "inputs/a-right.csv"},
                ],
            }
        ),
        encoding="utf-8",
    )

    plan = load_batch_manifest(manifest)

    assert plan.pairs == (
        BatchPair("alpha", left_a.resolve(), right_a.resolve()),
        BatchPair("zeta", left_z.resolve(), right_z.resolve()),
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"version": 2, "pairs": []}, "unsupported batch manifest version"),
        ({"version": 1, "pairs": []}, "must be a non-empty array"),
        (
            {
                "version": 1,
                "pairs": [
                    {"name": "same", "left": "left.csv", "right": "right.csv"},
                    {"name": "SAME", "left": "left2.csv", "right": "right2.csv"},
                ],
            },
            "duplicate batch pair name",
        ),
    ],
)
def test_manifest_validation_is_strict(tmp_path: Path, payload: object, message: str) -> None:
    for name in ("left.csv", "right.csv", "left2.csv", "right2.csv"):
        write_file(tmp_path, name, "id\n1\n")
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BatchError, match=message):
        load_batch_manifest(manifest)


def test_manifest_rejects_excessive_json_nesting_deterministically(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.json"
    nested = "[" * MAX_JSON_NESTING_DEPTH + "0" + "]" * MAX_JSON_NESTING_DEPTH
    manifest.write_text(f'{{"version": 1, "extra": {nested}}}', encoding="utf-8")

    with pytest.raises(BatchError, match="exceeds the maximum nesting depth"):
        load_batch_manifest(manifest)


def test_manifest_rejects_invalid_input_paths_as_batch_errors(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "pairs": [
                    {
                        "name": "invalid",
                        "left": "bad\x00path.csv",
                        "right": "right.csv",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BatchError, match=r"pairs\[0\]\.left input path is invalid"):
        load_batch_manifest(manifest)


def test_batch_execution_and_report_aggregate_pair_and_unmatched_issues(tmp_path: Path) -> None:
    clean_left = write_file(tmp_path, "clean-left.csv", "id,value\n1,A\n")
    clean_right = write_file(tmp_path, "clean-right.csv", "id,value\n1,A\n")
    changed_left = write_file(tmp_path, "changed-left.csv", "id,value\n1,A\n")
    changed_right = write_file(tmp_path, "changed-right.csv", "id,value\n1,B\n")
    unmatched = write_file(tmp_path, "unmatched.csv", "id,value\n1,A\n")
    plan = BatchPlan(
        (
            BatchPair("changed", changed_left, changed_right),
            BatchPair("clean", clean_left, clean_right),
        ),
        unmatched_left=(unmatched,),
    )

    result = reconcile_batch(plan, ReconciliationConfig(key_fields=("id",)))
    report = batch_result_to_dict(result)

    assert not result.is_clean
    assert [pair["name"] for pair in report["pairs"]] == ["changed", "clean"]
    assert report["status"] == "issues_found"
    assert report["summary"] == {
        "paired_inputs": 2,
        "clean_pairs": 1,
        "pairs_with_issues": 1,
        "matched_records": 2,
        "discrepancies": 1,
        "validation_violations": 0,
        "unmatched_left": 1,
        "unmatched_right": 0,
        "issues": 2,
    }
    destination = tmp_path / "reports" / "batch-report.json"
    assert write_json_batch_report(result, destination) == destination.resolve()
    assert json.loads(destination.read_text(encoding="utf-8")) == report


def test_cli_batch_compares_directories_and_returns_one_for_unmatched_input(
    tmp_path: Path, capsys
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    write_file(left, "paired.csv", "id,value\n1,A\n")
    write_file(right, "paired.csv", "id,value\n1,A\n")
    unmatched = write_file(left, "only-left.csv", "id,value\n1,A\n")

    status = main(["batch", str(left), str(right), "--key", "id"])

    report = json.loads(capsys.readouterr().out)
    assert status == 1
    assert report["summary"]["paired_inputs"] == 1
    assert report["unmatched_inputs"]["left"] == [str(unmatched.resolve())]


def test_cli_batch_supports_manifest_and_project_configuration(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,value\n1,A\n")
    right = write_file(tmp_path, "right.csv", "id,value\n1,A\n")
    manifest = tmp_path / "batch.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "pairs": [{"name": "one", "left": str(left), "right": str(right)}],
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "reconforge.json"
    config.write_text(
        json.dumps({"version": 1, "reconciliation": {"key_fields": ["id"]}}),
        encoding="utf-8",
    )

    status = main(["batch", "--manifest", str(manifest), "--config", str(config)])

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["status"] == "clean"
    assert report["pairs"][0]["name"] == "one"


def test_cli_batch_rejects_mixed_manifest_and_directory_modes(tmp_path: Path, capsys) -> None:
    manifest = tmp_path / "batch.json"
    manifest.write_text('{"version": 1, "pairs": []}', encoding="utf-8")

    status = main(
        ["batch", str(tmp_path), str(tmp_path), "--manifest", str(manifest), "--key", "id"]
    )

    assert status == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_cli_batch_reports_output_filesystem_failures(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    write_file(left, "orders.csv", "id,value\n1,A\n")
    write_file(right, "orders.csv", "id,value\n1,A\n")

    def fail_write(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(cli_module, "write_report", fail_write)

    status = main(
        [
            "batch",
            str(left),
            str(right),
            "--key",
            "id",
            "--output",
            str(tmp_path / "out.json"),
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == "reconforge: error: simulated disk failure\n"
