"""The demo scenario, end to end, on freshly generated example files."""

from pathlib import Path

from syncline.diff import compute
from syncline.engine import change_log, guard_violations, run
from syncline.spec import Guard
from syncline.state import State


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


def test_guard_stops_a_truncated_file(
    acme: Path, day1: Path, truncated: Path, tmp_path: Path
) -> None:
    flow, state = acme / "acme_users.yaml", State(tmp_path / "state.db")
    run(flow, file=day1, state=state)

    stopped = run(flow, file=truncated, state=state)
    assert stopped.status == "guarded"
    assert stopped.record.message and "above the limit of 20%" in stopped.record.message
    assert state.last_ok("acme_users").sha256 != stopped.record.sha256  # type: ignore[union-attr]

    preview = run(flow, file=truncated, dry_run=True, state=state)
    assert preview.status == "dry_run" and preview.warnings  # a dry run warns, doesn't stop

    forced = run(flow, file=truncated, allow_mass_change=True, state=state)
    assert forced.status == "ok" and forced.record.deactivated > 100


def test_guard_ratios() -> None:
    import polars as pl

    from syncline.io.read import ROW

    cur = pl.DataFrame(
        {"id": [str(i) for i in range(10)], "status": ["active"] * 8 + ["inactive"] * 2}
    )
    new = pl.DataFrame({"id": ["0", "1", "2", "3", "4"], "status": ["active"] * 5}).with_row_index(
        ROW
    )
    d = compute(new, cur, ["id"], {"status": "inactive"})
    assert d.counts()["deactivate"] == 3 and d.already_inactive == 2
    # 3 of the 8 active records (37%): the 2 already inactive don't count
    assert guard_violations(d, cur.height, Guard()) == [
        "would deactivate 3 of 8 records (38%), above the limit of 20%"
    ]
    assert guard_violations(d, cur.height, Guard(max_deactivate_ratio=0.5)) == []
    assert guard_violations(d, 0, Guard()) == []  # first load
    upd = compute(new.with_columns(pl.lit("x").alias("status")), cur, ["id"], None)
    assert guard_violations(upd, cur.height, Guard(max_update_ratio=0.1))


def test_change_log_records_old_and_new(acme: Path, day1: Path, tmp_path: Path) -> None:
    flow, state = acme / "acme_users.yaml", State(tmp_path / "state.db")
    run(flow, file=day1, state=state)
    res = run(flow, state=state)
    assert res.diff is not None
    log = change_log(res.diff)
    assert [c["action"] for c in log].count("deactivate") == 4
    upd = next(c for c in log if c["action"] == "update")
    assert set(upd["detail"]) == {"department"} and len(upd["detail"]["department"]) == 2
    stored = state.changes(res.record.id)
    assert len(stored) == 12 and stored[0]["key"]  # 6 + 2 + 4, kept in the run history
