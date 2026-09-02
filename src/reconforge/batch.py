"""Deterministic discovery and execution of reconciliation batches."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path

from reconforge.errors import BatchError
from reconforge.model import ReconciliationResult
from reconforge.readers import DatasetReadOptions, read_dataset, supported_extensions
from reconforge.reconcile import ReconciliationConfig, reconcile

BATCH_MANIFEST_VERSION = 1
_MANIFEST_KEYS = frozenset({"version", "pairs"})
_PAIR_KEYS = frozenset({"name", "left", "right"})


@dataclass(frozen=True, slots=True)
class BatchPair:
    """One explicitly named pair of input files."""

    name: str
    left: Path
    right: Path

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("batch pair names must be non-empty strings")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "left", Path(self.left))
        object.__setattr__(self, "right", Path(self.right))


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """Matched pairs and files that could not be paired."""

    pairs: tuple[BatchPair, ...]
    unmatched_left: tuple[Path, ...] = ()
    unmatched_right: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        pairs = tuple(self.pairs)
        if any(not isinstance(pair, BatchPair) for pair in pairs):
            raise TypeError("batch plan pairs must be BatchPair instances")
        names = [pair.name.casefold() for pair in pairs]
        if len(names) != len(set(names)):
            raise ValueError("batch pair names must be unique")
        object.__setattr__(
            self, "pairs", tuple(sorted(pairs, key=lambda pair: _sort_text(pair.name)))
        )
        object.__setattr__(
            self,
            "unmatched_left",
            tuple(sorted((Path(path) for path in self.unmatched_left), key=_sort_path)),
        )
        object.__setattr__(
            self,
            "unmatched_right",
            tuple(sorted((Path(path) for path in self.unmatched_right), key=_sort_path)),
        )


@dataclass(frozen=True, slots=True)
class BatchPairResult:
    """The result for one named batch pair."""

    pair: BatchPair
    result: ReconciliationResult

    def __post_init__(self) -> None:
        if not isinstance(self.pair, BatchPair):
            raise TypeError("batch pair result pair must be a BatchPair instance")
        if not isinstance(self.result, ReconciliationResult):
            raise TypeError("batch pair result result must be a ReconciliationResult instance")


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Complete outcome for a batch, including unpaired inputs."""

    pairs: tuple[BatchPairResult, ...]
    unmatched_left: tuple[Path, ...] = ()
    unmatched_right: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        pairs = tuple(self.pairs)
        if any(not isinstance(item, BatchPairResult) for item in pairs):
            raise TypeError("batch result pairs must be BatchPairResult instances")
        names = [item.pair.name.casefold() for item in pairs]
        if len(names) != len(set(names)):
            raise ValueError("batch result pair names must be unique")
        object.__setattr__(
            self,
            "pairs",
            tuple(sorted(pairs, key=lambda item: _sort_text(item.pair.name))),
        )
        object.__setattr__(
            self,
            "unmatched_left",
            tuple(sorted((Path(path) for path in self.unmatched_left), key=_sort_path)),
        )
        object.__setattr__(
            self,
            "unmatched_right",
            tuple(sorted((Path(path) for path in self.unmatched_right), key=_sort_path)),
        )

    @property
    def is_clean(self) -> bool:
        return (
            not self.unmatched_left
            and not self.unmatched_right
            and all(item.result.is_clean for item in self.pairs)
        )

    @property
    def issue_count(self) -> int:
        return (
            sum(item.result.issue_count for item in self.pairs)
            + len(self.unmatched_left)
            + len(self.unmatched_right)
        )


def discover_directory_batch(left: str | Path, right: str | Path) -> BatchPlan:
    """Pair recognized files recursively by their extension-free relative path."""

    left_root = _require_directory(Path(left), "left")
    right_root = _require_directory(Path(right), "right")
    left_files = _discover_side(left_root, "left")
    right_files = _discover_side(right_root, "right")
    if not left_files and not right_files:
        choices = ", ".join(supported_extensions())
        raise BatchError(
            f"batch directories contain no recognized datasets; expected extensions: {choices}"
        )

    shared = sorted(set(left_files) & set(right_files), key=_sort_text)
    pairs = tuple(
        BatchPair(
            name=left_files[key][0],
            left=left_files[key][1],
            right=right_files[key][1],
        )
        for key in shared
    )
    unmatched_left = tuple(left_files[key][1] for key in set(left_files) - set(right_files))
    unmatched_right = tuple(right_files[key][1] for key in set(right_files) - set(left_files))
    return BatchPlan(pairs, unmatched_left, unmatched_right)


def load_batch_manifest(path: str | Path) -> BatchPlan:
    """Load a strictly validated, versioned JSON batch manifest."""

    source = Path(path)
    payload = _read_manifest(source)
    _reject_unknown_keys(payload, _MANIFEST_KEYS, source, "top level")
    if payload.get("version") != BATCH_MANIFEST_VERSION or type(payload.get("version")) is not int:
        raise BatchError(
            f"{source}: unsupported batch manifest version {payload.get('version')!r}; "
            f"expected {BATCH_MANIFEST_VERSION}"
        )
    raw_pairs = payload.get("pairs")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise BatchError(f"{source}: 'pairs' must be a non-empty array")

    pairs: list[BatchPair] = []
    seen_names: set[str] = set()
    seen_left: set[Path] = set()
    seen_right: set[Path] = set()
    for index, raw_pair in enumerate(raw_pairs):
        location = f"pairs[{index}]"
        if not isinstance(raw_pair, dict):
            raise BatchError(f"{source}: {location} must be an object")
        _reject_unknown_keys(raw_pair, _PAIR_KEYS, source, location)
        name = _required_string(raw_pair, "name", source, location)
        name_key = name.casefold()
        if name_key in seen_names:
            raise BatchError(f"{source}: duplicate batch pair name {name!r}")
        left = _resolve_manifest_path(
            source, _required_string(raw_pair, "left", source, location), location, "left"
        )
        right = _resolve_manifest_path(
            source, _required_string(raw_pair, "right", source, location), location, "right"
        )
        if left in seen_left:
            raise BatchError(f"{source}: left input is assigned more than once: {left}")
        if right in seen_right:
            raise BatchError(f"{source}: right input is assigned more than once: {right}")
        seen_names.add(name_key)
        seen_left.add(left)
        seen_right.add(right)
        pairs.append(BatchPair(name, left, right))
    return BatchPlan(tuple(pairs))


def reconcile_batch(
    plan: BatchPlan,
    config: ReconciliationConfig,
    *,
    left_options: DatasetReadOptions | None = None,
    right_options: DatasetReadOptions | None = None,
) -> BatchResult:
    """Execute every pair in stable order and retain explicit unmatched inputs."""

    results = tuple(
        BatchPairResult(
            pair=pair,
            result=reconcile(
                read_dataset(pair.left, left_options),
                read_dataset(pair.right, right_options),
                config,
            ),
        )
        for pair in plan.pairs
    )
    return BatchResult(results, plan.unmatched_left, plan.unmatched_right)


def _require_directory(path: Path, side: str) -> Path:
    if not path.exists():
        raise BatchError(f"{side} batch directory does not exist: {path}")
    if not path.is_dir():
        raise BatchError(f"{side} batch input is not a directory: {path}")
    return path.resolve()


def _discover_side(root: Path, side: str) -> dict[str, tuple[str, Path]]:
    extensions = frozenset(supported_extensions())
    discovered: dict[str, tuple[str, Path]] = {}
    ambiguous: dict[str, list[Path]] = {}
    files = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in extensions),
        key=_sort_path,
    )
    for path in files:
        relative = path.relative_to(root)
        name = relative.with_suffix("").as_posix()
        key = name.casefold()
        if key in discovered:
            ambiguous.setdefault(key, [discovered[key][1]]).append(path)
        else:
            discovered[key] = (name, path.resolve())
    if ambiguous:
        details = "; ".join(
            f"{key!r}: {', '.join(str(path) for path in sorted(paths, key=_sort_path))}"
            for key, paths in sorted(ambiguous.items())
        )
        raise BatchError(f"ambiguous {side} batch identities: {details}")
    return discovered


class _DuplicateObjectKey(ValueError):
    pass


def _read_manifest(source: Path) -> dict[str, object]:
    try:
        text = source.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise BatchError(f"batch manifest does not exist: {source}") from exc
    except (OSError, UnicodeError) as exc:
        raise BatchError(f"cannot read batch manifest {source}: {exc}") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except _DuplicateObjectKey as exc:
        raise BatchError(f"{source}: {exc}") from exc
    except JSONDecodeError as exc:
        raise BatchError(
            f"{source}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise BatchError(f"{source}: top-level JSON value must be an object")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateObjectKey(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_unknown_keys(
    payload: Mapping[str, object],
    allowed: frozenset[str],
    source: Path,
    location: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise BatchError(
            f"{source}: unknown key(s) in {location}: " + ", ".join(repr(key) for key in unknown)
        )


def _required_string(payload: Mapping[str, object], key: str, source: Path, location: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BatchError(f"{source}: {location}.{key} must be a non-empty string")
    return value.strip()


def _resolve_manifest_path(source: Path, value: str, location: str, side: str) -> Path:
    candidate = Path(value)
    resolved = (
        (source.parent / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    if not resolved.exists():
        raise BatchError(f"{source}: {location}.{side} input does not exist: {resolved}")
    if not resolved.is_file():
        raise BatchError(f"{source}: {location}.{side} input is not a file: {resolved}")
    return resolved


def _sort_text(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _sort_path(path: Path) -> tuple[str, str]:
    value = path.as_posix()
    return value.casefold(), value
