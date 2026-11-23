"""Run history: one row per run, plus the rejected rows of each run.

Kept in its own small SQLite database (not in the target), so history survives
whatever happens to the target and never mixes with business data.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

DEFAULT_STATE = Path(os.environ.get("SYNCLINE_STATE", Path.home() / ".syncline" / "state.db"))

_meta = sa.MetaData()
runs = sa.Table(
    "runs",
    _meta,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("flow", sa.String, nullable=False, index=True),
    sa.Column("file", sa.String),
    sa.Column("sha256", sa.String),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.String, nullable=False),  # ok | dry_run | skipped | rejected | error
    sa.Column("total", sa.Integer, default=0),
    sa.Column("created", sa.Integer, default=0),
    sa.Column("updated", sa.Integer, default=0),
    sa.Column("deactivated", sa.Integer, default=0),
    sa.Column("unchanged", sa.Integer, default=0),
    sa.Column("rejected", sa.Integer, default=0),
    sa.Column("message", sa.Text),
)
rejected_rows = sa.Table(
    "rejected_rows",
    _meta,
    sa.Column("run_id", sa.String, sa.ForeignKey("runs.id"), primary_key=True),
    sa.Column("row", sa.Integer, primary_key=True),
    sa.Column("reasons", sa.Text, nullable=False),  # JSON list
    sa.Column("source_values", sa.Text, nullable=False),  # JSON object, as read from the file
)


@dataclass(frozen=True)
class RunRecord:
    id: str
    flow: str
    file: str | None
    sha256: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    total: int
    created: int
    updated: int
    deactivated: int
    unchanged: int
    rejected: int
    message: str | None


def now() -> datetime:
    return datetime.now(UTC)


class State:
    def __init__(self, path: Path = DEFAULT_STATE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = sa.create_engine(f"sqlite:///{path}")
        _meta.create_all(self.engine)

    def save(self, record: RunRecord, rejected: list[dict[str, Any]] | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(runs.insert(), [record.__dict__])
            if rejected:
                conn.execute(
                    rejected_rows.insert(),
                    [
                        {
                            "run_id": record.id,
                            "row": r["row"],
                            "reasons": json.dumps(r["reasons"], ensure_ascii=False),
                            "source_values": json.dumps(
                                r["values"], ensure_ascii=False, default=str
                            ),
                        }
                        for r in rejected
                    ],
                )

    def last_ok(self, flow: str) -> RunRecord | None:
        stmt = (
            sa.select(runs)
            .where(runs.c.flow == flow, runs.c.status == "ok")
            .order_by(runs.c.started_at.desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return RunRecord(**row) if row else None

    def history(self, flow: str | None = None, limit: int = 20) -> list[RunRecord]:
        stmt = sa.select(runs).order_by(runs.c.started_at.desc()).limit(limit)
        if flow:
            stmt = stmt.where(runs.c.flow == flow)
        with self.engine.connect() as conn:
            return [RunRecord(**r) for r in conn.execute(stmt).mappings()]

    def get(self, run_id: str) -> tuple[RunRecord, list[dict[str, Any]]] | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(runs).where(runs.c.id == run_id)).mappings().first()
            if not row:
                return None
            rej = conn.execute(
                sa.select(rejected_rows)
                .where(rejected_rows.c.run_id == run_id)
                .order_by(rejected_rows.c.row)
            ).mappings()
            rejected = [
                {
                    "row": r["row"],
                    "reasons": json.loads(r["reasons"]),
                    "values": json.loads(r["source_values"]),
                }
                for r in rej
            ]
        return RunRecord(**row), rejected
