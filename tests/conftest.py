"""Shared fixtures: a fresh copy of the Acme example in a temp folder, with its own databases."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real ~/.syncline: history and archive go to the test's temp folder."""
    monkeypatch.setenv("SYNCLINE_STATE", str(tmp_path / "default-state.db"))
    monkeypatch.setenv("SYNCLINE_ARCHIVE", str(tmp_path / "default-archive"))


@pytest.fixture
def acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """examples/acme regenerated in tmp, APP_DB_URL pointing at a temp SQLite file."""
    d = tmp_path / "acme"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_examples.py"), str(d)],
        check=True, capture_output=True,
    )  # fmt: skip
    shutil.copy(ROOT / "examples" / "acme" / "acme_users.yaml", d)
    monkeypatch.setenv("APP_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    return d


@pytest.fixture
def day1(acme: Path) -> Path:
    return acme / "employes_2026-11-01.csv"


@pytest.fixture
def truncated(acme: Path) -> Path:
    """Day 2 cut after 120 lines: the classic half-exported file."""
    src = acme / "employes_2026-11-02.csv"
    out = acme.parent / "truncated.csv"
    out.write_bytes(b"\n".join(src.read_bytes().split(b"\n")[:120]) + b"\n")
    return out
