from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from reconforge.exporters import ReportFormat
from reconforge.model import Dataset, Record
from reconforge.profile_report import render_profile_report, write_profile_report
from reconforge.profiling import ProfileOptions, profile_dataset, profile_from_dict


def make_profile(*, source: str = "ledger.csv"):
    headers = ("id", "amount", "comment")
    rows = (
        ("A", "-3", "<script>alert(1)</script>"),
        ("B", "2.50", '=HYPERLINK("bad")'),
        ("C", "2.50", "قيمة"),
        ("D", None, None),
    )
    dataset = Dataset(
        source,
        headers,
        tuple(
            Record(source, index + 2, dict(zip(headers, values, strict=True)))
            for index, values in enumerate(rows)
        ),
    )
    return profile_dataset(dataset, ProfileOptions(top_values_limit=3, examples_limit=2))


def test_json_report_is_deterministic_canonical_and_utf8_safe() -> None:
    profile = make_profile()

    first = render_profile_report(profile, ReportFormat.JSON)
    second = render_profile_report(profile, "json")

    assert first == second
    assert first.endswith("\n")
    assert "قيمة" in first
    assert profile_from_dict(json.loads(first)) == profile
    assert "recorded_at" not in first


def test_csv_report_has_one_row_per_field_and_formula_protection() -> None:
    profile = make_profile(source="=external.csv")

    rendered = render_profile_report(profile, ReportFormat.CSV)
    rows = list(csv.DictReader(io.StringIO(rendered)))

    assert [row["field"] for row in rows] == ["id", "amount", "comment"]
    assert all(row["source"] == "'=external.csv" for row in rows)
    assert rows[1]["record_count"] == "4"
    assert rows[1]["integer_values"] == "1"
    assert rows[1]["decimal_values"] == "2"
    assert rows[1]["numeric_minimum"] == "'-3"
    assert rows[1]["numeric_maximum"] == "2.5"
    assert json.loads(rows[2]["top_values"])[0] == {
        "count": 1,
        "value": "<script>alert(1)</script>",
    }
    assert rows[0]["fingerprint"].startswith("sha256:")


def test_csv_formula_protects_dangerous_field_names() -> None:
    source = "source.csv"
    dataset = Dataset(source, ("+calculation",), (Record(source, 2, {"+calculation": "x"}),))

    row = next(csv.DictReader(io.StringIO(render_profile_report(profile_dataset(dataset), "csv"))))

    assert row["field"] == "'+calculation"


def test_html_report_is_self_contained_escaped_and_deterministic() -> None:
    profile = make_profile(source='<img src=x onerror="bad">.csv')

    rendered = render_profile_report(profile, ReportFormat.HTML)

    assert rendered == render_profile_report(profile, "html")
    assert rendered.startswith('<!doctype html>\n<html lang="en">')
    assert "<style>" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "&lt;img src=x onerror=&quot;bad&quot;&gt;.csv" in rendered
    assert "https://" not in rendered
    assert "recorded_at" not in rendered
    assert "sha256:" in rendered


def test_html_escapes_dangerous_field_names() -> None:
    source = "source.csv"
    dataset = Dataset(source, ("<field>",), (Record(source, 2, {"<field>": "value"}),))

    rendered = render_profile_report(profile_dataset(dataset), "html")

    assert "<field>" not in rendered
    assert "&lt;field&gt;" in rendered


@pytest.mark.parametrize("format", list(ReportFormat))
def test_writer_is_atomic_creates_parents_and_returns_resolved_path(
    tmp_path: Path,
    format: ReportFormat,
) -> None:
    profile = make_profile()
    destination = tmp_path / "nested" / f"profile.{format.value}"

    returned = write_profile_report(profile, destination, format)

    assert returned == destination.resolve()
    assert destination.read_text(encoding="utf-8") == render_profile_report(profile, format)
    assert not list(destination.parent.glob(f".{destination.name}.*.tmp"))


def test_empty_dataset_still_renders_a_field_row() -> None:
    profile = profile_dataset(Dataset("empty.csv", ("id",), ()))

    row = next(csv.DictReader(io.StringIO(render_profile_report(profile, "csv"))))

    assert row["field"] == "id"
    assert row["record_count"] == "0"
    assert row["min_length"] == ""
    assert row["top_values"] == "[]"


def test_renderer_rejects_unknown_formats_and_wrong_types() -> None:
    profile = make_profile()

    with pytest.raises(ValueError, match="expected one of: json, csv, html"):
        render_profile_report(profile, "yaml")
    with pytest.raises(TypeError, match="ReportFormat or string"):
        render_profile_report(profile, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DatasetProfile"):
        render_profile_report(object(), "json")  # type: ignore[arg-type]
