from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from reconforge.atomic_io import atomic_write_text


def test_atomic_write_text_creates_parent_and_preserves_unicode(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "report.txt"

    returned = atomic_write_text(destination, "café قيمة\n")

    assert returned == destination.resolve()
    assert destination.read_bytes() == "café قيمة\n".encode()
    assert not list(destination.parent.glob(f".{destination.name}.*.tmp"))


def test_atomic_write_text_preserves_destination_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "report.txt"
    destination.write_text("previous\n", encoding="utf-8")

    def fail_replace(source: object, target: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replacement failure"):
        atomic_write_text(destination, "replacement\n")

    assert destination.read_text(encoding="utf-8") == "previous\n"
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_atomic_write_text_uses_collision_safe_staging_files(tmp_path: Path) -> None:
    destination = tmp_path / "shared.txt"
    payloads = tuple(f"writer-{index}:" + (str(index) * 10_000) for index in range(8))

    with ThreadPoolExecutor(max_workers=8) as executor:
        returned = tuple(
            executor.map(lambda value: atomic_write_text(destination, value), payloads)
        )

    assert all(path == destination.resolve() for path in returned)
    assert destination.read_text(encoding="utf-8") in payloads
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_atomic_write_text_rejects_non_text_before_creating_directories(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "report.txt"

    with pytest.raises(TypeError, match="payload must be a string"):
        atomic_write_text(destination, b"bytes")  # type: ignore[arg-type]

    assert not destination.parent.exists()
