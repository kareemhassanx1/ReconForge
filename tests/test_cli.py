from __future__ import annotations

import json
from pathlib import Path

from reconforge.cli import main


def write_file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_compare_returns_zero_and_prints_clean_json(
    tmp_path: Path,
    capsys,
) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10\n")

    status = main(["compare", str(left), str(right), "--key", "id"])

    captured = capsys.readouterr()
    assert status == 0
    assert json.loads(captured.out)["status"] == "clean"
    assert captured.err == ""


def test_compare_returns_one_and_writes_difference_report(
    tmp_path: Path,
    capsys,
) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,12\n")
    output = tmp_path / "report.json"

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "differences_found" in captured.out
    assert "1 discrepancy" in captured.out
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "differences_found"


def test_compare_returns_two_for_invalid_input(tmp_path: Path, capsys) -> None:
    right = write_file(tmp_path, "right.csv", "id,amount\nA,12\n")

    status = main(["compare", str(tmp_path / "missing.csv"), str(right), "--key", "id"])

    captured = capsys.readouterr()
    assert status == 2
    assert "does not exist" in captured.err


def test_cli_supports_composite_keys_and_selected_fields(
    tmp_path: Path,
    capsys,
) -> None:
    left = write_file(
        tmp_path,
        "left.csv",
        "account,period,amount,note\n100,2026-08,10,old\n",
    )
    right = write_file(
        tmp_path,
        "right.csv",
        "account,period,amount,note\n100,2026-08,10,new\n",
    )

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "account",
            "--key",
            "period",
            "--compare",
            "amount",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["configuration"]["key_fields"] == ["account", "period"]
    assert report["configuration"]["compared_fields"] == ["amount"]


def test_cli_can_match_keys_without_case_sensitivity(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nInvoice-A,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\ninvoice-a,10\n")

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--ignore-key-case",
        ]
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out)["summary"]["matched_records"] == 1


def test_cli_applies_typed_field_normalizers(tmp_path: Path, capsys) -> None:
    left = write_file(
        tmp_path,
        "left.csv",
        "id,amount,settled_on,customer\n0007,012.500,2026-08-21,CAFÉ   STORE\n",
    )
    right = write_file(
        tmp_path,
        "right.csv",
        "id,amount,settled_on,customer\n7,12.5,2026-08-21,café store\n",
    )

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--normalize",
            "id=integer",
            "--normalize",
            "amount=decimal",
            "--normalize",
            "settled_on=date",
            "--normalize",
            "customer=text",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["status"] == "clean"
    assert report["configuration"]["field_normalizers"] == {
        "amount": "decimal",
        "customer": "text",
        "id": "integer",
        "settled_on": "date",
    }


def test_cli_returns_two_for_invalid_normalizer_syntax(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10\n")

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--normalize",
            "amount",
        ]
    )

    assert status == 2
    assert "expected FIELD=TYPE" in capsys.readouterr().err


def test_cli_reports_normalization_source_row_field_and_value(
    tmp_path: Path,
    capsys,
) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,not-money\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10\n")

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--normalize",
            "amount=decimal",
        ]
    )

    error = capsys.readouterr().err
    assert status == 2
    assert "left.csv row 2 field 'amount' value 'not-money' as decimal" in error


def test_cli_applies_per_field_comparison_policies(tmp_path: Path, capsys) -> None:
    left = write_file(
        tmp_path,
        "left.csv",
        "id,amount,settled_on,customer\nA,10.000,2026-08-23,CAFÉ   STORE\n",
    )
    right = write_file(
        tmp_path,
        "right.csv",
        "id,amount,settled_on,customer\nA,10.009,20260823,café store\n",
    )

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--policy",
            "amount=decimal:0.01",
            "--policy",
            "settled_on=date",
            "--policy",
            "customer=text",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["status"] == "clean"
    assert report["configuration"]["comparison_policies"] == {
        "amount": "decimal:tolerance=0.01",
        "customer": "text",
        "settled_on": "date",
    }


def test_cli_decimal_policy_reports_values_outside_tolerance(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,10.000\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10.011\n")

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--policy",
            "amount=decimal:0.01",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 1
    assert report["differences"][0]["left_value"] == "10.000"
    assert report["differences"][0]["right_value"] == "10.011"


def test_cli_returns_two_for_invalid_policy_syntax(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10\n")

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--policy",
            "amount",
        ]
    )

    assert status == 2
    assert "expected FIELD=SPEC" in capsys.readouterr().err


def test_cli_reports_policy_source_row_field_and_value(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,not-money\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10\n")

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--policy",
            "amount=decimal:0.01",
        ]
    )

    error = capsys.readouterr().err
    assert status == 2
    assert "left.csv row 2 field 'amount' value 'not-money' for decimal:tolerance=0.01" in error


def test_cli_reconciles_csv_against_json_using_detected_formats(
    tmp_path: Path,
    capsys,
) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount,active\nA,10.00,true\n")
    right = write_file(
        tmp_path,
        "right.json",
        '[{"id": "A", "amount": 10, "active": true}]',
    )

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--key",
            "id",
            "--normalize",
            "amount=decimal",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["status"] == "clean"


def test_cli_can_set_different_explicit_formats_for_each_side(
    tmp_path: Path,
    capsys,
) -> None:
    left = write_file(tmp_path, "left.data", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.data", '{"id": "A", "amount": 10}\n')

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--left-format",
            "csv",
            "--right-format",
            "ndjson",
            "--key",
            "id",
        ]
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"


def test_cli_returns_two_when_format_cannot_be_detected(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.data", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10\n")

    status = main(["compare", str(left), str(right), "--key", "id"])

    assert status == 2
    assert "cannot detect dataset format" in capsys.readouterr().err


def test_cli_returns_two_for_unknown_explicit_format(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.data", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.data", "id,amount\nA,10\n")

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--format",
            "xml",
            "--key",
            "id",
        ]
    )

    assert status == 2
    assert "expected one of: csv, json, ndjson" in capsys.readouterr().err
