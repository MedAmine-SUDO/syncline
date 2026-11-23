"""SQL target: any database SQLAlchemy supports (SQLite and Postgres are tested).

Writes are driven by the diff (plain INSERT and UPDATE ... WHERE key = ...), all in one
transaction. That needs no unique constraint or dialect-specific upsert, re-running the
same file writes nothing, and a failure leaves the table exactly as it was.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import polars as pl
import sqlalchemy as sa

from syncline.diff import Diff
from syncline.io.read import ROW
from syncline.spec import FieldMapping, FieldType, SqlTarget

_SA_TYPES: dict[FieldType, Any] = {
    "string": sa.Text,
    "email": sa.Text,
    "int": sa.BigInteger,
    "float": sa.Float,
    "bool": sa.Boolean,
    "date": sa.Date,
}
_PL_TYPES: dict[FieldType, Any] = {
    "string": pl.String,
    "email": pl.String,
    "int": pl.Int64,
    "float": pl.Float64,
    "bool": pl.Boolean,
    "date": pl.Date,
}


class TargetError(Exception):
    """The target can't be reached or doesn't match the flow."""


def resolve_url(url: str) -> str:
    if url.startswith("env:"):
        name = url.removeprefix("env:")
        value = os.environ.get(name)
        if not value:
            raise TargetError(f"environment variable {name} is not set (target url)")
        return value
    return url


@dataclass
class SqlTable:
    engine: sa.Engine
    table: sa.Table | None  # None: doesn't exist yet (dry run on a new target)
    schema: dict[str, Any]  # polars types of the managed columns

    @classmethod
    def open(
        cls,
        target: SqlTarget,
        mapping: dict[str, FieldMapping],
        key: list[str],
        extra: dict[str, Any] | None = None,
        create: bool = True,
    ) -> SqlTable:
        """Connect and find the table.

        If it doesn't exist: create it from the mapping (key = primary key) when `create`
        is true, otherwise return a handle that reads as empty (for dry runs).
        """
        try:
            engine = sa.create_engine(resolve_url(target.url))
            meta = sa.MetaData()
            exists = sa.inspect(engine).has_table(target.table)
        except sa.exc.SQLAlchemyError as e:
            raise TargetError(f"can't connect to the target: {e}") from None
        except ModuleNotFoundError as e:
            raise TargetError(
                f"missing database driver: {e.name} (try syncline[postgres])"
            ) from None

        schema: dict[str, Any] = {f: _PL_TYPES[m.type] for f, m in mapping.items()}
        for f in extra or {}:
            schema.setdefault(f, pl.String)

        table: sa.Table | None = None
        if exists:
            table = sa.Table(target.table, meta, autoload_with=engine)
            missing = [c for c in schema if c not in table.c]
            if missing:
                raise TargetError(f"table {target.table} has no column(s): {', '.join(missing)}")
        elif create:
            cols = [
                sa.Column(
                    f, _SA_TYPES[mapping[f].type] if f in mapping else sa.Text, primary_key=f in key
                )
                for f in schema
            ]
            table = sa.Table(target.table, meta, *cols)
            meta.create_all(engine)
        return cls(engine=engine, table=table, schema=schema)

    def read(self, key: list[str]) -> pl.DataFrame:
        """Current rows: the managed columns only, typed like the incoming frame."""
        if self.table is None:
            return pl.DataFrame(schema=self.schema)
        cols = [self.table.c[c] for c in self.schema]
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(*cols)).all()
        df = pl.DataFrame([tuple(r) for r in rows], schema=self.schema, orient="row")
        return df.sort(key)

    def apply(self, diff: Diff) -> None:
        """Write the diff in one transaction."""
        if self.table is None:
            raise TargetError("the target table doesn't exist")
        key = diff.key
        t = self.table
        create = diff.create.drop(ROW).select(list(self._writable(diff.create)))
        with self.engine.begin() as conn:
            if not create.is_empty():
                conn.execute(t.insert(), _records(create))
            if not diff.update.is_empty():
                fields = [c for c in self._writable(diff.update) if c not in key]
                stmt = (
                    t.update()
                    .where(sa.and_(*(t.c[k] == sa.bindparam(f"_k_{k}") for k in key)))
                    .values({f: sa.bindparam(f) for f in fields})
                )
                upd = diff.update.select(
                    *[pl.col(k).alias(f"_k_{k}") for k in key], *[pl.col(f) for f in fields]
                )
                conn.execute(stmt, _records(upd))
            if not diff.deactivate.is_empty():
                stmt = (
                    t.update()
                    .where(sa.and_(*(t.c[k] == sa.bindparam(f"_k_{k}") for k in key)))
                    .values(diff.deactivate_set)
                )
                deact = diff.deactivate.select(pl.col(k).alias(f"_k_{k}") for k in key)
                conn.execute(stmt, _records(deact))

    def _writable(self, df: pl.DataFrame) -> list[str]:
        return [c for c in df.columns if c in self.schema]


def _records(df: pl.DataFrame) -> list[dict[str, Any]]:
    return df.to_dicts()
