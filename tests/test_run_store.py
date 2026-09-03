from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import reconforge.run_store as run_store_module
from reconforge.cli import main
from reconforge.errors import RunNotFoundError, RunStoreError
from reconforge.run_store import (
    RUN_STORE_SCHEMA_VERSION,
    RunInput,
    RunStore,
    stored_run_to_dict,
)


def write_file(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def record_clean_run(store: RunStore, *, command: str = "compare"):
    return store.record_run(
        command=command,
        status="clean",
        summary={"issues": 0, "matched_records": 1},
        metadata={"report_version": 1},
        inputs=(RunInput("left", "left.csv"), RunInput("right", "right.csv")),
    )


def test_new_store_migrates_to_current_version_and_creates_schema(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "history.sqlite"

    with RunStore(database) as store:
        assert store.schema_version == RUN_STORE_SCHEMA_VERSION

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"runs", "run_inputs"} <= tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_store_rejects_a_schema_newer_than_the_runtime_without_modifying_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "future.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(RunStoreError, match="schema version 99"):
        RunStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
            ).fetchone()
            is None
        )


def test_failed_migration_rolls_back_its_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "broken-migration.sqlite"
    monkeypatch.setattr(
        run_store_module,
        "_MIGRATIONS",
        {1: ("CREATE TABLE migration_probe (id INTEGER)", "NOT VALID SQL")},
    )

    with pytest.raises(RunStoreError, match="cannot migrate.*version 1"):
        RunStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
            ).fetchone()
            is None
        )


def test_record_and_lookup_preserve_metadata_input_order_and_utc_time(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    local_time = datetime(2026, 8, 27, 18, 45, 30, 123456, tzinfo=timezone(timedelta(hours=3)))

    with RunStore(database, clock=lambda: local_time) as store:
        recorded = store.record_run(
            command="batch",
            status="issues_found",
            summary={"issues": 2, "paired_inputs": 1},
            metadata={"pairs": [{"name": "august", "status": "clean"}]},
            inputs=(
                RunInput("left", "left.csv", "august"),
                RunInput("right", "right.json", "august"),
                RunInput("left", "unmatched.csv"),
            ),
        )
        loaded = store.get_run(recorded.id)

    assert loaded.id == 1
    assert loaded.recorded_at == "2026-08-27T15:45:30.123Z"
    assert loaded.command == "batch"
    assert loaded.status == "issues_found"
    assert dict(loaded.summary) == {"issues": 2, "paired_inputs": 1}
    assert dict(loaded.metadata) == {"pairs": [{"name": "august", "status": "clean"}]}
    assert loaded.inputs == (
        RunInput("left", "left.csv", "august"),
        RunInput("right", "right.json", "august"),
        RunInput("left", "unmatched.csv"),
    )


def test_repeat_runs_receive_distinct_ids_and_list_newest_first(tmp_path: Path) -> None:
    with RunStore(tmp_path / "history.sqlite") as store:
        first = record_clean_run(store)
        second = record_clean_run(store)

        assert first.id == 1
        assert second.id == 2
        assert [run.id for run in store.list_runs()] == [2, 1]
        assert [run.id for run in store.list_runs(limit=1, offset=1)] == [1]


def test_run_and_inputs_are_rolled_back_together_when_an_input_insert_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite"
    with RunStore(database) as store:
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_right_input
                BEFORE INSERT ON run_inputs
                WHEN NEW.side = 'right'
                BEGIN
                    SELECT RAISE(ABORT, 'right input rejected');
                END
                """
            )

        with pytest.raises(RunStoreError, match="right input rejected"):
            record_clean_run(store)

        assert store.list_runs() == ()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM run_inputs").fetchone()[0] == 0


def test_lookup_reports_missing_run_and_closed_store(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "history.sqlite")
    with pytest.raises(RunNotFoundError, match="run 8 was not found"):
        store.get_run(8)
    store.close()

    with pytest.raises(RunStoreError, match="run store is closed"):
        store.list_runs()


def test_open_existing_rejects_missing_store_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "history.sqlite"

    with pytest.raises(RunStoreError, match="run store does not exist"):
        RunStore(database, create=False)

    assert not database.exists()
    assert not database.parent.exists()


def test_open_existing_reads_a_previously_created_store(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    with RunStore(database) as store:
        expected = record_clean_run(store)

    with RunStore(database, create=False) as store:
        assert store.get_run(expected.id) == expected


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"command": ""}, "command must be a non-empty string"),
        ({"inputs": ()}, "requires at least one input"),
        ({"summary": {"bad": float("nan")}}, "JSON-compatible values"),
    ],
)
def test_record_validation_happens_before_a_transaction(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    arguments: dict[str, object] = {
        "command": "compare",
        "status": "clean",
        "summary": {"issues": 0},
        "inputs": (RunInput("left", "left.csv"),),
    }
    arguments.update(kwargs)
    with RunStore(tmp_path / "history.sqlite") as store:
        with pytest.raises((TypeError, ValueError), match=message):
            store.record_run(**arguments)  # type: ignore[arg-type]
        assert store.list_runs() == ()


def test_stored_run_serialization_can_omit_metadata(tmp_path: Path) -> None:
    with RunStore(tmp_path / "history.sqlite") as store:
        run = record_clean_run(store)

    compact = stored_run_to_dict(run, include_metadata=False)
    assert "metadata" not in compact
    assert compact["inputs"] == [
        {"side": "left", "source": "left.csv", "pair_name": None},
        {"side": "right", "source": "right.csv", "pair_name": None},
    ]


def test_cli_compare_records_repeat_runs_and_history_commands(tmp_path: Path, capsys) -> None:
    left = write_file(tmp_path, "left.csv", "id,value\n1,A\n")
    right = write_file(tmp_path, "right.csv", "id,value\n1,A\n")
    database = tmp_path / "history.sqlite"

    for _ in range(2):
        assert (
            main(
                [
                    "compare",
                    str(left),
                    str(right),
                    "--key",
                    "id",
                    "--history-db",
                    str(database),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["status"] == "clean"

    assert main(["history", "list", str(database), "--limit", "1"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["schema_version"] == 1
    assert len(listed["runs"]) == 1
    assert listed["runs"][0]["id"] == 2
    assert "metadata" not in listed["runs"][0]

    assert main(["history", "show", str(database), "1"]) == 0
    shown = json.loads(capsys.readouterr().out)["run"]
    assert shown["id"] == 1
    assert shown["command"] == "compare"
    assert shown["metadata"]["report_version"] == 1
    assert [item["side"] for item in shown["inputs"]] == ["left", "right"]


def test_cli_batch_records_pair_and_unmatched_inputs(tmp_path: Path, capsys) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    write_file(left, "paired.csv", "id,value\n1,A\n")
    write_file(right, "paired.csv", "id,value\n1,A\n")
    write_file(left, "unmatched.csv", "id,value\n1,A\n")
    database = tmp_path / "history.sqlite"

    status = main(
        [
            "batch",
            str(left),
            str(right),
            "--key",
            "id",
            "--history-db",
            str(database),
        ]
    )
    capsys.readouterr()

    assert status == 1
    with RunStore(database) as store:
        run = store.get_run(1)
    assert run.command == "batch"
    assert run.status == "issues_found"
    assert [(item.side, item.pair_name) for item in run.inputs] == [
        ("left", "paired"),
        ("right", "paired"),
        ("left", None),
    ]


def test_cli_history_returns_two_for_missing_run(tmp_path: Path, capsys) -> None:
    database = tmp_path / "history.sqlite"
    with RunStore(database):
        pass

    status = main(["history", "show", str(database), "99"])

    assert status == 2
    assert "run 99 was not found" in capsys.readouterr().err


def test_cli_history_does_not_create_a_missing_database(tmp_path: Path, capsys) -> None:
    database = tmp_path / "missing" / "history.sqlite"

    status = main(["history", "list", str(database)])

    assert status == 2
    assert "run store does not exist" in capsys.readouterr().err
    assert not database.exists()
    assert not database.parent.exists()


def test_cli_history_validates_pagination_bounds(tmp_path: Path, capsys) -> None:
    database = tmp_path / "history.sqlite"

    status = main(["history", "list", str(database), "--limit", "0"])

    assert status == 2
    assert "limit must be an integer from 1 to 1000" in capsys.readouterr().err
