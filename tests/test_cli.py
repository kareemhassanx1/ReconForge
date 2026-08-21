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
