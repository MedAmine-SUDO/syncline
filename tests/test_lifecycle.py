"""Lifecycle rules: allowed states and transitions, on creates, updates and deactivations."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import sqlalchemy as sa
import yaml

from syncline.diff import compute
from syncline.engine import run
from syncline.io.read import ROW
from syncline.lifecycle import enforce
from syncline.spec import Lifecycle, SpecError, load_flow
from syncline.state import State

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "trainings"

LC = Lifecycle(
    field="status",
    initial=["draft", "published"],
    transitions={
        "draft": ["published", "cancelled"],
        "published": ["archived", "cancelled"],
        "archived": [],
        "cancelled": [],
    },
)


PUBLISHED_TO_DRAFT = (
    "status can't go from 'published' to 'draft' (from 'published': 'archived', 'cancelled')"
)


def incoming(rows: list[tuple[int, str, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {ROW: [r[0] for r in rows], "code": [r[1] for r in rows], "status": [r[2] for r in rows]},
        schema={ROW: pl.UInt32, "code": pl.String, "status": pl.String},
    )


def target(rows: list[tuple[str, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"code": [r[0] for r in rows], "status": [r[1] for r in rows]},
        schema={"code": pl.String, "status": pl.String},
    )


def reasons(result: Any) -> dict[int, str]:
    return {r[ROW]: r["reasons"][0] for r in result.rejected.iter_rows(named=True)}


def test_creates_must_start_in_an_initial_state() -> None:
    diff = compute(
        incoming([(2, "A", "draft"), (3, "B", "archived"), (4, "C", "weird")]), target([]), ["code"]
    )
    out = enforce(diff, target([]), LC)
    assert out.diff.create["code"].to_list() == ["A"]
    assert reasons(out) == {
        3: "a new record can't start as status 'archived' (allowed: 'draft', 'published')",
        4: "unknown status 'weird' (states: 'draft', 'published', 'archived', 'cancelled')",
    }


def test_updates_follow_the_transitions() -> None:
    current = target(
        [("A", "draft"), ("B", "published"), ("C", "archived"), ("D", "legacy"), ("E", None)]
    )
    file = incoming(
        [
            (2, "A", "published"),
            (3, "B", "draft"),
            (4, "C", "published"),
            (5, "D", "draft"),
            (6, "E", "draft"),
        ]
    )
    out = enforce(compute(file, current, ["code"]), current, LC)
    assert sorted(out.diff.update["code"].to_list()) == [
        "A",
        "E",
    ]  # E: no state yet = like a create
    assert reasons(out) == {
        3: PUBLISHED_TO_DRAFT,
        4: "status can't go from 'archived' to 'published' (from 'archived': none)",
        5: "the target's current status 'legacy' is not a lifecycle state; fix it there first",
    }


def test_a_status_cant_be_emptied_and_unchanged_status_is_fine() -> None:
    current = target([("A", "published"), ("B", "archived")])
    out = enforce(
        compute(incoming([(2, "A", None), (3, "B", "archived")]), current, ["code"]), current, LC
    )
    assert reasons(out) == {2: "status can't be emptied (it is 'published')"}
    assert out.diff.update.is_empty()


def test_forbidden_deactivations_are_left_alone_and_reported() -> None:
    current = target([("A", "published"), ("B", "draft"), ("C", "draft"), ("D", None)])
    diff = compute(incoming([]), current, ["code"], {"status": "archived"})
    out = enforce(diff, current, LC)
    assert sorted(out.diff.deactivate["code"].to_list()) == ["A", "D"]
    assert out.kept == [{"code": "B"}, {"code": "C"}]
    assert out.diff.unchanged == diff.unchanged + 2
    (w,) = out.warnings
    assert w.startswith("2 record(s) missing from the file were not deactivated")
    assert "(2 draft): B, C" in w


def test_many_kept_records_are_summarised() -> None:
    current = target([(f"K{i}", "draft") for i in range(7)])
    out = enforce(compute(incoming([]), current, ["code"], {"status": "archived"}), current, LC)
    assert out.warnings[0].endswith("K0, K1, K2, K3, K4 and 2 more")


def test_any_initial_state_when_initial_is_not_given() -> None:
    lc = Lifecycle(field="status", transitions={"open": ["closed"], "closed": []})
    out = enforce(compute(incoming([(2, "A", "closed")]), target([]), ["code"]), target([]), lc)
    assert out.rejected.is_empty()


# --- the trainings example, end to end ---------------------------------------------------


@pytest.fixture
def trainings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "trainings"
    shutil.copytree(EXAMPLE, d)
    monkeypatch.setenv("APP_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    return d


def rows(tmp_path: Path) -> dict[str, dict[str, Any]]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with engine.connect() as c:
        return {r.code: r._asdict() for r in c.execute(sa.text("select * from trainings"))}


def test_trainings_example(trainings: Path, tmp_path: Path) -> None:
    state = State(tmp_path / "s.db")
    flow = trainings / "trainings.yaml"
    day1 = run(flow, file=trainings / "formations_2026-11-01.csv", state=state)
    assert (day1.status, day1.record.created, day1.record.rejected) == ("ok", 10, 0)

    preview = run(flow, dry_run=True, state=state)
    day2 = run(flow, state=state)
    assert preview.diff is not None and day2.diff is not None
    assert preview.diff.counts() == day2.diff.counts()  # the dry run is what gets applied
    r = day2.record
    assert (r.status, r.created, r.updated, r.deactivated, r.rejected) == ("ok", 1, 3, 1, 2)
    assert {x["row"]: x["reasons"] for x in day2.rejected} == {
        8: [PUBLISHED_TO_DRAFT],
        10: ["a new record can't start as status 'archived' (allowed: 'draft', 'published')"],
    }
    assert "TRN-010" in (r.message or "")  # the draft that wasn't archived

    db = rows(tmp_path)
    assert db["TRN-008"]["status"] == "published"
    assert db["TRN-008"]["starts_on"] == "2026-10-05"  # rejected as a whole: no partial update
    assert db["TRN-010"]["status"] == "draft"
    assert db["TRN-004"]["status"] == "archived"
    assert "TRN-011" not in db


def test_lifecycle_rejections_count_towards_the_rejected_ratio(
    trainings: Path, tmp_path: Path
) -> None:
    flow = trainings / "trainings.yaml"
    data = yaml.safe_load(flow.read_text(encoding="utf-8"))
    data["contract"]["max_rejected_ratio"] = 0.1
    flow.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    state = State(tmp_path / "s.db")
    run(flow, file=trainings / "formations_2026-11-01.csv", state=state)
    before = rows(tmp_path)
    day2 = run(flow, state=state)
    assert day2.status == "rejected" and day2.record.rejected == 2
    assert "2 of 10 rows rejected" in (day2.record.message or "")
    assert rows(tmp_path) == before  # nothing written


# --- flow validation ---------------------------------------------------------------------


def problems(tmp_path: Path, **lifecycle: Any) -> str:
    data = yaml.safe_load((EXAMPLE / "trainings.yaml").read_text(encoding="utf-8"))
    data["lifecycle"].update(lifecycle)
    p = tmp_path / "f.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SpecError) as e:
        load_flow(p)
    return " ".join(e.value.problems)


def test_states_must_all_be_declared(tmp_path: Path) -> None:
    assert "unknown state(s) closed, open" in problems(
        tmp_path, transitions={"draft": ["open"], "published": []}, initial=["closed"]
    )


def test_lifecycle_field_rules(tmp_path: Path) -> None:
    assert "lifecycle.field: phase not in mapping" in problems(tmp_path, field="phase")
    assert "can't be a key field" in problems(tmp_path, field="code")


def test_deactivation_value_must_be_a_state(tmp_path: Path) -> None:
    data = yaml.safe_load((EXAMPLE / "trainings.yaml").read_text(encoding="utf-8"))
    data["load"]["deactivate"]["set"]["status"] = "gone"
    p = tmp_path / "f.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SpecError, match="'gone' is not a lifecycle state"):
        load_flow(p)
