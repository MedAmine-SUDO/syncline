"""Run history: one row per run, plus its rejected rows and its change log.

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


def default_state() -> Path:
    """SYNCLINE_STATE, or ~/.syncline/state.db. Read at call time, not import time."""
    return Path(os.environ.get("SYNCLINE_STATE", Path.home() / ".syncline" / "state.db"))


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
    sa.Column("flow_path", sa.String),  # the flow file used (for replay)
    sa.Column("archived", sa.String),  # where the input file was kept (for replay)
)
rejected_rows = sa.Table(
    "rejected_rows",
    _meta,
    sa.Column("run_id", sa.String, sa.ForeignKey("runs.id"), primary_key=True),
    sa.Column("row", sa.Integer, primary_key=True),
    sa.Column("reasons", sa.Text, nullable=False),  # JSON list
    sa.Column("source_values", sa.Text, nullable=False),  # JSON object, as read from the file
)
changes = sa.Table(
    "changes",
    _meta,
    sa.Column("run_id", sa.String, sa.ForeignKey("runs.id"), primary_key=True),
    sa.Column("seq", sa.Integer, primary_key=True),
    sa.Column("action", sa.String, nullable=False),  # create | update | deactivate
    sa.Column("record_key", sa.Text, nullable=False),  # JSON object: key field -> value
    sa.Column("detail", sa.Text, nullable=False),  # JSON: values, {field: [old, new]} or set
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
    flow_path: str | None = None
    archived: str | None = None


def now() -> datetime:
    return datetime.now(UTC)


class State:
    def __init__(self, path: Path | None = None) -> None:
        path = path or default_state()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = sa.create_engine(f"sqlite:///{path}")
        _meta.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that newer versions store to a history created by an older one."""
        with self.engine.begin() as conn:
            for table in _meta.sorted_tables:
                existing = {c["name"] for c in sa.inspect(conn).get_columns(table.name)}
                for column in table.columns:
                    if column.name not in existing:
                        ddl = column.type.compile(dialect=conn.dialect)
                        conn.execute(
                            sa.text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
                        )

    def save(
        self,
        record: RunRecord,
        rejected: list[dict[str, Any]] | None = None,
        change_log: list[dict[str, Any]] | None = None,
    ) -> None:
        """Store a run, its rejected rows and its changes (applied, or previewed by a dry run)."""
        with self.engine.begin() as conn:
            conn.execute(runs.insert(), [record.__dict__])
            if change_log:
                conn.execute(
                    changes.insert(),
                    [
                        {
                            "run_id": record.id,
                            "seq": i,
                            "action": c["action"],
                            "record_key": _dumps(c["key"]),
                            "detail": _dumps(c["detail"]),
                        }
                        for i, c in enumerate(change_log)
                    ],
                )
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

    def changes(self, run_id: str) -> list[dict[str, Any]]:
        stmt = sa.select(changes).where(changes.c.run_id == run_id).order_by(changes.c.seq)
        with self.engine.connect() as conn:
            return [
                {
                    "action": r["action"],
                    "key": json.loads(r["record_key"]),
                    "detail": json.loads(r["detail"]),
                }
                for r in conn.execute(stmt).mappings()
            ]

    def find(self, run_id: str) -> RunRecord | None:
        """A run by id, or by a unique prefix of its id."""
        stmt = sa.select(runs).where(runs.c.id.startswith(run_id, autoescape=True)).limit(2)
        with self.engine.connect() as conn:
            rows = list(conn.execute(stmt).mappings())
        return RunRecord(**rows[0]) if len(rows) == 1 else None

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


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
