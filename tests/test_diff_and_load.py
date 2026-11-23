from datetime import date
from pathlib import Path

import polars as pl
import pytest
import sqlalchemy as sa

from syncline.diff import compute
from syncline.io.read import ROW
from syncline.spec import FieldMapping, SqlTarget
from syncline.targets.sql import SqlTable, TargetError, resolve_url

SCHEMA = {"id": pl.String, "dept": pl.String, "hired": pl.Date, "status": pl.String}
MAPPING = {
    "id": FieldMapping.model_validate({"from": "id"}),
    "dept": FieldMapping.model_validate({"from": "dept"}),
    "hired": FieldMapping.model_validate({"from": "hired", "type": "date"}),
    "status": FieldMapping.model_validate({"default": "active"}),
}
OFF = {"status": "inactive"}


def incoming(*rows: tuple[str, str, date]) -> pl.DataFrame:
    df = pl.DataFrame([(i, d, h, "active") for i, d, h in rows], schema=SCHEMA, orient="row")
    return df.with_row_index(ROW, offset=2)


def current(*rows: tuple[str, str, date, str]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=SCHEMA, orient="row")


D = date(2024, 1, 1)


def test_diff_create_update_deactivate_unchanged() -> None:
    new = incoming(("A", "Sales", D), ("B", "HR", D), ("D", "Ops", D))
    cur = current(
        ("A", "Sales", D, "active"), ("B", "Sales", D, "active"), ("C", "HR", D, "active")
    )
    d = compute(new, cur, ["id"], OFF)
    assert d.counts() == {"create": 1, "update": 1, "deactivate": 1, "unchanged": 1}
    assert d.update["__changed"].to_list() == [["dept"]]
    assert d.update["old_dept"].to_list() == ["Sales"]
    assert d.deactivate["id"].to_list() == ["C"]


def test_already_inactive_is_not_deactivated_again() -> None:
    d = compute(incoming(("A", "Sales", D)), current(("Z", "HR", D, "inactive")), ["id"], OFF)
    assert d.deactivate.is_empty()
    assert d.unchanged == 1


def test_returning_person_is_reactivated() -> None:
    d = compute(incoming(("Z", "HR", D)), current(("Z", "HR", D, "inactive")), ["id"], OFF)
    assert d.update["__changed"].to_list() == [["status"]]


def test_rejected_row_never_deactivates_its_record() -> None:
    cur = current(("A", "Sales", D, "active"), ("B", "HR", D, "active"))
    present = pl.DataFrame({"id": ["A", "B", None]})  # B is in the file, but its row was rejected
    d = compute(incoming(("A", "Sales", D)), cur, ["id"], OFF, present=present)
    assert d.deactivate.is_empty()


def test_null_versus_value_counts_as_a_change() -> None:
    new = incoming(("A", "Sales", D)).with_columns(pl.lit(None, pl.String).alias("dept"))
    d = compute(new, current(("A", "Sales", D, "active")), ["id"], OFF)
    assert d.update["__changed"].to_list() == [["dept"]]


def open_table(tmp_path: Path, create: bool = True) -> SqlTable:
    target = SqlTarget(url=f"sqlite:///{tmp_path / 'app.db'}", table="users")
    return SqlTable.open(target, MAPPING, ["id"], extra=OFF, create=create)


def test_sql_roundtrip_is_idempotent(tmp_path: Path) -> None:
    t = open_table(tmp_path)
    new = incoming(("A", "Sales", D), ("B", "HR", D))
    t.apply(compute(new, t.read(["id"]), ["id"], OFF))
    assert t.read(["id"]).to_dicts() == new.drop(ROW).to_dicts()
    again = compute(new, t.read(["id"]), ["id"], OFF)
    assert again.is_empty and again.unchanged == 2

    later = incoming(("A", "Finance", D))
    t.apply(compute(later, t.read(["id"]), ["id"], OFF))
    rows = {r["id"]: r for r in t.read(["id"]).to_dicts()}
    assert rows["A"]["dept"] == "Finance"
    assert rows["B"]["status"] == "inactive"


def test_failed_load_writes_nothing(tmp_path: Path) -> None:
    t = open_table(tmp_path)
    good = incoming(("A", "Sales", D))
    t.apply(compute(good, t.read(["id"]), ["id"], OFF))
    bad = incoming(("B", "HR", D), ("B", "HR", D))  # same primary key twice: insert fails
    with pytest.raises(sa.exc.IntegrityError):
        t.apply(compute(bad, t.read(["id"]), ["id"], OFF))
    assert t.read(["id"])["id"].to_list() == ["A"]  # the whole transaction rolled back


def test_dry_run_on_missing_table_creates_nothing(tmp_path: Path) -> None:
    t = open_table(tmp_path, create=False)
    assert t.table is None and t.read(["id"]).is_empty()
    assert not sa.inspect(sa.create_engine(f"sqlite:///{tmp_path / 'app.db'}")).has_table("users")


def test_existing_table_missing_column(tmp_path: Path) -> None:
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with eng.begin() as c:
        c.execute(sa.text("create table users (id text primary key, dept text)"))
    with pytest.raises(TargetError, match="no column"):
        open_table(tmp_path)


def test_env_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_DB", "sqlite://")
    assert resolve_url("env:APP_DB") == "sqlite://"
    monkeypatch.delenv("APP_DB")
    with pytest.raises(TargetError, match="APP_DB is not set"):
        resolve_url("env:APP_DB")
