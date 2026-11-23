"""The demo scenario, end to end, on freshly generated example files."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from syncline.engine import run
from syncline.state import State

ROOT = Path(__file__).parent.parent


@pytest.fixture
def acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "acme"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_examples.py"), str(d)], check=True)
    shutil.copy(ROOT / "examples" / "acme" / "acme_users.yaml", d)
    monkeypatch.setenv("APP_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    return d


def test_demo_scenario(acme: Path, tmp_path: Path) -> None:
    flow, state = acme / "acme_users.yaml", State(tmp_path / "state.db")
    day1 = acme / "employes_2026-11-01.csv"

    preview = run(flow, file=day1, dry_run=True, state=state)
    applied = run(flow, file=day1, state=state)
    assert preview.status == "dry_run" and applied.status == "ok"
    assert preview.diff is not None and applied.diff is not None
    assert preview.diff.counts() == applied.diff.counts()  # preview == what gets applied
    assert applied.diff.counts()["create"] == 311
    assert len(applied.rejected) == 12
    assert all(r["reasons"] and r["values"]["Matricule"] for r in applied.rejected)

    assert run(flow, file=day1, state=state).status == "skipped"
    forced = run(flow, file=day1, force=True, state=state)
    assert forced.diff is not None and forced.diff.is_empty

    day2 = run(flow, state=state)  # glob picks the 2026-11-02 file
    assert day2.diff is not None
    assert day2.diff.counts() == {"create": 6, "update": 2, "deactivate": 4, "unchanged": 305}
    assert [s.status for s in state.history()] == ["ok", "ok", "skipped", "ok", "dry_run"]


def test_too_many_rejections_load_nothing(acme: Path, tmp_path: Path) -> None:
    flow = acme / "acme_users.yaml"
    flow.write_text(
        flow.read_text().replace("max_rejected_ratio: 0.05", "max_rejected_ratio: 0.01")
    )
    res = run(flow, state=State(tmp_path / "state.db"))
    assert res.status == "rejected"
    assert res.diff is None  # stopped before the target was touched
