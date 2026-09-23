"""Check the tracked wheel payload and exercise an isolated installed CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from glob import glob
from pathlib import Path


def verify(wheel: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z", "--", "src/reconforge"], cwd=root)
        .decode()
        .split("\0")
    )
    expected = {name.removeprefix("src/"): root / name for name in tracked if name}
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), "duplicate wheel entries"
        packaged = {name for name in names if ".dist-info/" not in name}
        assert packaged == set(expected), "wheel payload differs from tracked source paths"
        for name, source in expected.items():
            assert archive.read(name).replace(b"\r\n", b"\n") == source.read_bytes().replace(
                b"\r\n", b"\n"
            ), f"wheel content differs: {name}"

    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="reconforge-wheel-") as directory:
        work = Path(directory)
        venv.EnvBuilder(with_pip=True).create(work / "env")
        python = work / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            cwd=work,
            env=environment,
            check=True,
        )

        def cli(*args: str, status: int = 0) -> str:
            result = subprocess.run(
                [str(python), "-I", "-m", "reconforge", *args],
                cwd=work,
                env=environment,
                text=True,
                capture_output=True,
            )
            assert result.returncode == status, (args, result.stdout, result.stderr)
            return result.stdout

        (work / "left.csv").write_text("id,value\nA,old\n", encoding="utf-8")
        (work / "right.csv").write_text("id,value\nA,new\n", encoding="utf-8")
        cli(
            "compare",
            "left.csv",
            "right.csv",
            "--key",
            "id",
            "--output",
            "report.json",
            "--history-db",
            "history.sqlite",
            status=1,
        )
        report = json.loads((work / "report.json").read_text(encoding="utf-8"))
        assert report["summary"]["field_differences"] == 1
        cli("verify", "report.json")
        stored = json.loads(cli("history", "show", "history.sqlite", "1"))
        assert stored["run"]["command"] == "compare"
        cli("inspect", "report.json")
        (work / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "pairs": [{"name": "export", "left": "left.csv", "right": "right.csv"}],
                }
            ),
            encoding="utf-8",
        )
        for _ in range(2):
            cli(
                "batch",
                "--manifest",
                "manifest.json",
                "--key",
                "id",
                "--checkpoint",
                "checkpoint.json",
                status=1,
            )
    print(f"{wheel.name}: tracked payload and offline installed comparison/history/resume passed")


if __name__ == "__main__":
    wheels = [Path(name).resolve() for pattern in sys.argv[1:] for name in glob(pattern)]
    if not wheels:
        raise SystemExit("provide at least one wheel path")
    for wheel in wheels:
        verify(wheel)
