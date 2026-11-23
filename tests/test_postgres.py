"""The demo scenario against a real Postgres.

Runs when SYNCLINE_TEST_PG_URL is set (CI sets it; locally, see CONTRIBUTING.md):
    postgresql+psycopg://postgres:postgres@localhost:5432/postgres
"""

import os
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa

from syncline.engine import run
from syncline.state import State

PG_URL = os.environ.get("SYNCLINE_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="SYNCLINE_TEST_PG_URL not set")


@pytest.fixture
def pg_flow(acme: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    assert PG_URL
    table = f"users_{uuid.uuid4().hex[:8]}"
    flow = acme / "acme_users.yaml"
    flow.write_text(flow.read_text().replace("table: users", f"table: {table}"))
    monkeypatch.setenv("APP_DB_URL", PG_URL)
    yield flow, table
    with sa.create_engine(PG_URL).begin() as c:
        c.execute(sa.text(f'drop table if exists "{table}"'))


def test_demo_scenario_on_postgres(pg_flow: tuple[Path, str], day1: Path, tmp_path: Path) -> None:
    assert PG_URL
    flow, table = pg_flow
    state = State(tmp_path / "state.db")

    first = run(flow, file=day1, state=state)
    assert first.status == "ok" and first.record.created == 311

    second = run(flow, state=state)
    assert (second.record.created, second.record.updated, second.record.deactivated) == (6, 2, 4)

    again = run(flow, force=True, state=state)
    assert again.diff is not None and again.diff.is_empty  # idempotent on Postgres too

    with sa.create_engine(PG_URL).connect() as c:
        cols = {col["name"]: col["type"] for col in sa.inspect(c).get_columns(table)}
        assert isinstance(cols["hire_date"], sa.Date)
        pk = sa.inspect(c).get_pk_constraint(table)["constrained_columns"]
        assert pk == ["external_id"]
        row = c.execute(sa.text(f'select hire_date, status from "{table}" limit 1')).one()
        assert isinstance(row.hire_date, date)
        inactive = c.execute(sa.text(f"select count(*) from \"{table}\" where status = 'inactive'"))
        assert inactive.scalar() == 4
