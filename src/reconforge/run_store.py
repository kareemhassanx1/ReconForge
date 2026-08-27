"""Transactional SQLite persistence for reconciliation run metadata."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from types import MappingProxyType

from reconforge.errors import RunNotFoundError, RunStoreError

RUN_STORE_SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            command TEXT NOT NULL CHECK (length(command) > 0),
            status TEXT NOT NULL CHECK (length(status) > 0),
            summary_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE run_inputs (
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK (position >= 0),
            side TEXT NOT NULL CHECK (side IN ('left', 'right')),
            pair_name TEXT,
            source TEXT NOT NULL CHECK (length(source) > 0),
            PRIMARY KEY (run_id, position)
        )
        """,
    )
}


@dataclass(frozen=True, slots=True)
class RunInput:
    """One source involved in a stored comparison or batch."""

    side: str
    source: str
    pair_name: str | None = None

    def __post_init__(self) -> None:
        if self.side not in {"left", "right"}:
            raise ValueError("run input side must be 'left' or 'right'")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("run input source must be a non-empty string")
        if self.pair_name is not None and (
            not isinstance(self.pair_name, str) or not self.pair_name.strip()
        ):
            raise ValueError("run input pair name must be a non-empty string or null")
        object.__setattr__(self, "source", self.source.strip())
        if self.pair_name is not None:
            object.__setattr__(self, "pair_name", self.pair_name.strip())


@dataclass(frozen=True, slots=True)
class StoredRun:
    """Immutable run metadata reconstructed from the local store."""

    id: int
    recorded_at: str
    command: str
    status: str
    summary: Mapping[str, object]
    metadata: Mapping[str, object]
    inputs: tuple[RunInput, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", MappingProxyType(dict(self.summary)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "inputs", tuple(self.inputs))


class RunStore:
    """Versioned SQLite store with atomic run and input writes."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection: sqlite3.Connection | None = None
        self._open()

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        """Return the on-disk schema version for this open store."""

        connection = self._require_open()
        return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        """Close the SQLite connection; repeated calls are safe."""

        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def record_run(
        self,
        *,
        command: str,
        status: str,
        summary: Mapping[str, object],
        inputs: Sequence[RunInput],
        metadata: Mapping[str, object] | None = None,
        recorded_at: datetime | None = None,
    ) -> StoredRun:
        """Persist a run and all of its inputs in one transaction."""

        command = _required_text(command, "run command")
        status = _required_text(status, "run status")
        input_items = tuple(inputs)
        if not input_items:
            raise ValueError("a stored run requires at least one input")
        if any(not isinstance(item, RunInput) for item in input_items):
            raise TypeError("run inputs must be RunInput instances")
        summary_json = _encode_object(summary, "run summary")
        metadata_json = _encode_object(metadata or {}, "run metadata")
        timestamp = _format_timestamp(recorded_at or self._clock())
        connection = self._require_open()

        try:
            with self._transaction():
                cursor = connection.execute(
                    """
                    INSERT INTO runs (recorded_at, command, status, summary_json, metadata_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (timestamp, command, status, summary_json, metadata_json),
                )
                run_id = int(cursor.lastrowid)
                connection.executemany(
                    """
                    INSERT INTO run_inputs (run_id, position, side, pair_name, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (run_id, position, item.side, item.pair_name, item.source)
                        for position, item in enumerate(input_items)
                    ),
                )
        except sqlite3.Error as exc:
            raise RunStoreError(f"cannot record reconciliation run in {self.path}: {exc}") from exc
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> StoredRun:
        """Look up one run by its stable integer identifier."""

        if type(run_id) is not int or run_id < 1:
            raise ValueError("run id must be a positive integer")
        connection = self._require_open()
        try:
            row = connection.execute(
                """
                SELECT id, recorded_at, command, status, summary_json, metadata_json
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run {run_id} was not found in {self.path}")
            return self._stored_run(row)
        except sqlite3.Error as exc:
            raise RunStoreError(f"cannot read reconciliation run from {self.path}: {exc}") from exc

    def list_runs(self, *, limit: int = 20, offset: int = 0) -> tuple[StoredRun, ...]:
        """List newest runs first with bounded deterministic pagination."""

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("history limit must be an integer from 1 to 1000")
        if type(offset) is not int or offset < 0:
            raise ValueError("history offset must be a non-negative integer")
        connection = self._require_open()
        try:
            rows = connection.execute(
                """
                SELECT id, recorded_at, command, status, summary_json, metadata_json
                FROM runs ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return tuple(self._stored_run(row) for row in rows)
        except sqlite3.Error as exc:
            raise RunStoreError(
                f"cannot list reconciliation history from {self.path}: {exc}"
            ) from exc

    def _open(self) -> None:
        if self.path != Path(":memory:"):
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RunStoreError(
                    f"cannot create run store directory {self.path.parent}: {exc}"
                ) from exc
        try:
            connection = sqlite3.connect(self.path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            self._connection = connection
            self._migrate()
        except (OSError, sqlite3.Error, RunStoreError) as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if isinstance(exc, RunStoreError):
                raise
            raise RunStoreError(f"cannot open reconciliation run store {self.path}: {exc}") from exc

    def _migrate(self) -> None:
        connection = self._require_open()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > RUN_STORE_SCHEMA_VERSION:
            raise RunStoreError(
                f"run store {self.path} uses schema version {version}; "
                f"this ReconForge supports up to {RUN_STORE_SCHEMA_VERSION}"
            )
        for target in range(version + 1, RUN_STORE_SCHEMA_VERSION + 1):
            statements = _MIGRATIONS.get(target)
            if statements is None:
                raise RunStoreError(f"missing run store migration for schema version {target}")
            try:
                with self._transaction():
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {target}")
            except sqlite3.Error as exc:
                raise RunStoreError(
                    f"cannot migrate run store {self.path} to schema version {target}: {exc}"
                ) from exc

    def _stored_run(self, row: sqlite3.Row) -> StoredRun:
        connection = self._require_open()
        input_rows = connection.execute(
            """
            SELECT side, source, pair_name FROM run_inputs
            WHERE run_id = ? ORDER BY position
            """,
            (row["id"],),
        ).fetchall()
        return StoredRun(
            id=row["id"],
            recorded_at=row["recorded_at"],
            command=row["command"],
            status=row["status"],
            summary=_decode_object(row["summary_json"], self.path, "summary"),
            metadata=_decode_object(row["metadata_json"], self.path, "metadata"),
            inputs=tuple(
                RunInput(item["side"], item["source"], item["pair_name"]) for item in input_rows
            ),
        )

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RunStoreError("run store is closed")
        return self._connection

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        connection = self._require_open()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            _rollback(connection)
            raise
        else:
            try:
                connection.execute("COMMIT")
            except BaseException:
                _rollback(connection)
                raise


def stored_run_to_dict(run: StoredRun, *, include_metadata: bool = True) -> dict[str, object]:
    """Serialize a stored run for the CLI or another JSON consumer."""

    payload: dict[str, object] = {
        "id": run.id,
        "recorded_at": run.recorded_at,
        "command": run.command,
        "status": run.status,
        "summary": dict(run.summary),
        "inputs": [
            {
                "side": item.side,
                "source": item.source,
                "pair_name": item.pair_name,
            }
            for item in run.inputs
        ],
    }
    if include_metadata:
        payload["metadata"] = dict(run.metadata)
    return payload


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _encode_object(value: Mapping[str, object], label: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON-compatible values: {exc}") from exc


def _decode_object(value: str, path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except (JSONDecodeError, TypeError) as exc:
        raise RunStoreError(f"run store {path} contains invalid {label} JSON") from exc
    if not isinstance(payload, dict):
        raise RunStoreError(f"run store {path} contains a non-object {label}")
    return payload


def _format_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("run timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("run timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.execute("ROLLBACK")
