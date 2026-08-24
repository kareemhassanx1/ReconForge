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


def test_cli_runs_end_to_end_from_project_config_with_mixed_formats(
    tmp_path: Path,
    capsys,
) -> None:
    left = write_file(
        tmp_path,
        "accounting.data",
        "invoice_id,amount,settled_on,customer,exported_at\n"
        "0007,10.000,2026-08-24,CAFÉ   STORE,08:00\n",
    )
    right = write_file(
        tmp_path,
        "gateway.data",
        json.dumps(
            [
                {
                    "invoice_id": 7,
                    "amount": 10.009,
                    "settled_on": "20260824",
                    "customer": "café store",
                    "exported_at": "09:00",
                }
            ]
        ),
    )
    config = write_file(
        tmp_path,
        "reconforge.json",
        json.dumps(
            {
                "version": 1,
                "inputs": {
                    "format": "ndjson",
                    "left_format": "csv",
                    "right_format": "json",
                },
                "reconciliation": {
                    "key_fields": ["invoice_id"],
                    "compare_fields": ["amount", "settled_on", "customer"],
                    "ignore_fields": ["exported_at"],
                    "field_normalizers": {"invoice_id": "integer"},
                    "comparison_policies": {
                        "amount": "decimal:0.01",
                        "settled_on": "date",
                        "customer": "text",
                    },
                },
            }
        ),
    )

    status = main(["compare", str(left), str(right), "--config", str(config)])

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["status"] == "clean"
    assert report["configuration"]["key_fields"] == ["invoice_id"]
    assert report["configuration"]["comparison_policies"] == {
        "amount": "decimal:tolerance=0.01",
        "customer": "text",
        "settled_on": "date",
    }


def test_cli_options_replace_configured_scalars_and_lists(tmp_path: Path, capsys) -> None:
    left = write_file(
        tmp_path,
        "left.data",
        "id,amount,status,note\nA,10.000,paid,old\n",
    )
    right = write_file(
        tmp_path,
        "right.data",
        "id,amount,status,note\nA,10.009,pending,new\n",
    )
    config = write_file(
        tmp_path,
        "reconforge.json",
        json.dumps(
            {
                "version": 1,
                "inputs": {"format": "json", "delimiter": ";"},
                "reconciliation": {
                    "key_fields": ["missing_key"],
                    "compare_fields": ["status"],
                    "ignore_fields": ["amount"],
                    "field_normalizers": {"amount": "integer"},
                    "comparison_policies": {"amount": "exact"},
                },
            }
        ),
    )

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--config",
            str(config),
            "--format",
            "csv",
            "--delimiter",
            ",",
            "--key",
            "id",
            "--compare",
            "amount",
            "--ignore",
            "note",
            "--normalize",
            "amount=decimal",
            "--policy",
            "amount=decimal:0.01",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["configuration"]["key_fields"] == ["id"]
    assert report["configuration"]["compared_fields"] == ["amount"]
    assert report["configuration"]["ignored_fields"] == ["note"]
    assert report["configuration"]["field_normalizers"] == {"amount": "decimal"}
    assert report["configuration"]["comparison_policies"] == {"amount": "decimal:tolerance=0.01"}


def test_cli_side_format_overrides_shared_cli_and_config_formats(
    tmp_path: Path,
    capsys,
) -> None:
    left = write_file(tmp_path, "left.data", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.data", '{"id": "A", "amount": "10"}\n')
    config = write_file(
        tmp_path,
        "reconforge.json",
        json.dumps(
            {
                "version": 1,
                "inputs": {"format": "json", "right_format": "json"},
                "reconciliation": {"key_fields": ["id"]},
            }
        ),
    )

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--config",
            str(config),
            "--format",
            "csv",
            "--right-format",
            "ndjson",
        ]
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"


def test_cli_field_assignments_merge_with_config_by_field(tmp_path: Path, capsys) -> None:
    left = write_file(
        tmp_path,
        "left.csv",
        "id,amount,customer\nA,10.000,CAFÉ STORE\n",
    )
    right = write_file(
        tmp_path,
        "right.csv",
        "id,amount,customer\nA,10.009,café store\n",
    )
    config = write_file(
        tmp_path,
        "reconforge.json",
        json.dumps(
            {
                "version": 1,
                "reconciliation": {
                    "key_fields": ["id"],
                    "comparison_policies": {"customer": "text"},
                },
            }
        ),
    )

    status = main(
        [
            "compare",
            str(left),
            str(right),
            "--config",
            str(config),
            "--policy",
            "amount=decimal:0.01",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["configuration"]["comparison_policies"] == {
        "amount": "decimal:tolerance=0.01",
        "customer": "text",
    }


def test_cli_case_flag_overrides_project_configuration(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nInvoice-A,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\ninvoice-a,10\n")
    config = write_file(
        tmp_path,
        "reconforge.json",
        json.dumps(
            {
                "version": 1,
                "reconciliation": {
                    "key_fields": ["id"],
                    "case_sensitive_keys": False,
                },
            }
        ),
    )

    configured_status = main(["compare", str(left), str(right), "--config", str(config)])
    configured_report = json.loads(capsys.readouterr().out)
    overridden_status = main(
        [
            "compare",
            str(left),
            str(right),
            "--config",
            str(config),
            "--case-sensitive-keys",
        ]
    )
    overridden_report = json.loads(capsys.readouterr().out)

    assert configured_status == 0
    assert configured_report["summary"]["matched_records"] == 1
    assert configured_report["configuration"]["case_sensitive_keys"] is False
    assert overridden_status == 1
    assert overridden_report["summary"]["matched_records"] == 0
    assert overridden_report["configuration"]["case_sensitive_keys"] is True


def test_cli_returns_two_for_unknown_project_config_key(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10\n")
    config = write_file(
        tmp_path,
        "reconforge.json",
        json.dumps({"version": 1, "reconciliation": {"keys": ["id"]}}),
    )

    status = main(["compare", str(left), str(right), "--config", str(config)])

    assert status == 2
    assert "unknown key(s) in reconciliation: 'keys'" in capsys.readouterr().err


def test_cli_requires_keys_from_cli_or_project_config(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,amount\nA,10\n")
    right = write_file(tmp_path, "right.csv", "id,amount\nA,10\n")

    status = main(["compare", str(left), str(right)])

    assert status == 2
    assert "provide --key or reconciliation.key_fields" in capsys.readouterr().err


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
