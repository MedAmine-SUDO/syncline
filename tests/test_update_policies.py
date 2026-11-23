"""Per-field update policies: always (default), if_empty, never."""

from datetime import date
from pathlib import Path

import polars as pl
import pytest
import sqlalchemy as sa
import yaml

from syncline.diff import compute
from syncline.engine import run
from syncline.io.read import ROW
from syncline.spec import FieldMapping, SpecError, SqlTarget, load_flow
from syncline.state import State
from syncline.targets.sql import SqlTable

SCHEMA = {"id": pl.String, "dept": pl.String, "nick": pl.String}
POLICIES = {"dept": "if_empty", "nick": "never"}


def incoming(*rows: tuple[str, str | None, str | None]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=SCHEMA, orient="row").with_row_index(ROW, offset=2)


def current(*rows: tuple[str, str | None, str | None]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=SCHEMA, orient="row")


@pytest.mark.parametrize(
    ("old", "new", "changed"),
    [
        (("A", None, "Bob"), ("A", "Sales", "Bobby"), ["dept"]),  # if_empty fills a blank
        (("A", "HR", "Bob"), ("A", "Sales", "Bobby"), []),  # ...but never overwrites
        (("A", "HR", "Bob"), ("A", None, None), []),  # ...nor blanks a value
        (("A", "HR", None), ("A", "HR", "Bobby"), []),  # never: not even to fill a blank
    ],
)
def test_policies_decide_what_counts_as_a_change(
    old: tuple[str, str | None, str | None],
    new: tuple[str, str | None, str | None],
    changed: list[str],
) -> None:
    d = compute(incoming(new), current(old), ["id"], policies=POLICIES)  # type: ignore[arg-type]
    assert (d.update["__changed"].to_list() or [[]]) == [changed]
    assert d.unchanged == (0 if changed else 1)


def test_always_is_the_default() -> None:
    d = compute(incoming(("A", "Sales", "x")), current(("A", "HR", "x")), ["id"])
    assert d.update["__changed"].to_list() == [["dept"]]


def test_create_ignores_policies() -> None:
    d = compute(incoming(("B", "Sales", "Bobby")), current(), ["id"], policies=POLICIES)  # type: ignore[arg-type]
    assert d.create.select("dept", "nick").row(0) == ("Sales", "Bobby")


def table(tmp_path: Path) -> SqlTable:
    mapping = {f: FieldMapping.model_validate({"from": f}) for f in SCHEMA}
    return SqlTable.open(
        SqlTarget(url=f"sqlite:///{tmp_path / 'app.db'}", table="t"), mapping, ["id"]
    )


def test_update_writes_only_changed_fields(tmp_path: Path) -> None:
    t = table(tmp_path)
    t.apply(compute(incoming(("A", "HR", "Bob"), ("B", None, "Al")), t.read(["id"]), ["id"]))

    new = incoming(("A", "Sales", "Bobby"), ("B", "Ops", "Alan"))
    diff = compute(new, t.read(["id"]), ["id"], policies={"nick": "never"})
    # Someone edits A's nickname in the application after we read the table:
    with t.engine.begin() as c:
        c.execute(sa.text("update t set nick = 'Robert' where id = 'A'"))
    t.apply(diff)

    rows = {r["id"]: r for r in t.read(["id"]).to_dicts()}
    assert rows["A"] == {"id": "A", "dept": "Sales", "nick": "Robert"}  # the app's edit survives
    assert rows["B"] == {"id": "B", "dept": "Ops", "nick": "Al"}  # never: untouched


def test_groups_rows_by_changed_fields(tmp_path: Path) -> None:
    t = table(tmp_path)
    t.apply(
        compute(
            incoming(("A", "HR", "a"), ("B", "HR", "b"), ("C", "HR", "c")), t.read(["id"]), ["id"]
        )
    )
    new = incoming(("A", "Ops", "a"), ("B", "HR", "bb"), ("C", "Ops", "cc"))
    t.apply(compute(new, t.read(["id"]), ["id"]))
    assert t.read(["id"]).rows() == [("A", "Ops", "a"), ("B", "HR", "bb"), ("C", "Ops", "cc")]


def test_policy_on_key_is_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(
        (Path(__file__).parent.parent / "examples/acme/acme_users.yaml").read_text()
    )
    data["mapping"]["external_id"]["update"] = "never"
    p = tmp_path / "f.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SpecError, match="key fields can't have an update policy"):
        load_flow(p)


def test_never_policy_end_to_end(acme: Path, day1: Path, tmp_path: Path) -> None:
    """Day 2 moves two people; with `update: never` the app keeps its department."""
    flow = acme / "acme_users.yaml"
    data = yaml.safe_load(flow.read_text(encoding="utf-8"))
    data["mapping"]["department"]["update"] = "never"
    flow.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    state = State(tmp_path / "state.db")
    run(flow, file=day1, state=state)
    day2 = run(flow, state=state)
    assert (day2.record.created, day2.record.updated, day2.record.deactivated) == (6, 0, 4)
    assert day2.record.unchanged == 307


def test_hire_date_type_survives(tmp_path: Path) -> None:
    mapping = {
        "id": FieldMapping.model_validate({"from": "id"}),
        "d": FieldMapping.model_validate({"from": "d", "type": "date", "update": "if_empty"}),
    }
    t = SqlTable.open(SqlTarget(url=f"sqlite:///{tmp_path / 'a.db'}", table="t"), mapping, ["id"])
    first = pl.DataFrame({"id": ["A"], "d": [None]}, schema={"id": pl.String, "d": pl.Date})
    t.apply(compute(first.with_row_index(ROW), t.read(["id"]), ["id"]))
    later = pl.DataFrame({"id": ["A"], "d": [date(2024, 1, 2)]}).with_row_index(ROW)
    t.apply(compute(later, t.read(["id"]), ["id"], policies={"d": "if_empty"}))
    assert t.read(["id"])["d"].to_list() == [date(2024, 1, 2)]
