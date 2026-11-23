"""Lookups: fields whose value comes from another flow's records.

A participants file says "karim.saidi1@acme.example, TRN-003"; the target wants the person's
`external_id`, or the API's own id for that person. A lookup finds it in the target of the
flow that loads people, by matching the file's value:

    user_id: { from: E-mail, lookup: { flow: ../acme/acme_users.yaml, match: email,
                                       take: external_id, where: { status: active } } }

Each looked-up flow's target is read once per run. A value that matches nothing, or more than
one record, is a bad cell: the row is rejected with that reason, like a bad date. The value
taken is then typed with the field's `type`.

Flows that look each other up should run in order (`syncline run-all` sorts them); a record
created by another flow later is found by the next run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from syncline.io.read import ROW
from syncline.mapping.apply import ERROR_SCHEMA, Mapped, _typed
from syncline.spec import FlowSpec, Lookup, SpecError, load_flow
from syncline.targets.base import open_target


class LinkError(Exception):
    """A lookup can't be set up: missing or invalid flow file, unknown column, a cycle..."""


def lookup_path(lookup: Lookup, flow_path: Path) -> Path:
    return (flow_path.parent / lookup.flow).resolve()


def dependencies(spec: FlowSpec, flow_path: Path) -> dict[str, Path]:
    """Field → the flow file it looks up."""
    return {
        f: lookup_path(m.lookup, flow_path) for f, m in spec.mapping.items() if m.lookup is not None
    }


def load_linked(path: Path, spec: FlowSpec) -> FlowSpec:
    try:
        other = load_flow(path)
    except SpecError as e:
        raise LinkError(f"lookup flow {path}: {'; '.join(e.problems)}") from None
    if other.flow == spec.flow:
        raise LinkError(
            f"a flow can't look up its own records ({spec.flow}): records loaded by this run "
            "wouldn't be found yet"
        )
    return other


@dataclass
class _Tables:
    """Each looked-up target read once per run (per flow and set of columns)."""

    run_id: str
    cache: dict[tuple[Path, tuple[str, ...]], pl.DataFrame] = field(default_factory=dict)

    def get(self, path: Path, spec: FlowSpec, columns: list[str]) -> pl.DataFrame:
        cached = self.cache.get((path, tuple(columns)))
        if cached is not None:
            return cached
        other = load_linked(path, spec)
        deactivate = other.load.deactivate.set if other.load.deactivate else None
        target = open_target(
            other.load.target,
            other.mapping,
            other.load.key,
            extra=deactivate,
            create=False,
            run_id=self.run_id,
        )
        table = target.columns(columns)
        self.cache[(path, tuple(columns))] = table
        return table


def _where_text(value: object) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def resolve(mapped: Mapped, spec: FlowSpec, flow_path: Path, run_id: str) -> Mapped:
    """Replace each lookup field's file value by the value taken from the other flow."""
    links = dependencies(spec, flow_path)
    if not links:
        return mapped
    tables = _Tables(run_id)
    df, errors = mapped.df, [mapped.errors]
    for name, path in links.items():
        m = spec.mapping[name]
        lk = m.lookup
        assert lk is not None
        other = load_linked(path, spec)
        columns = list(dict.fromkeys([lk.match, lk.result, *lk.where]))
        table = tables.get(path, spec, columns)
        for f, v in lk.where.items():
            table = table.filter(pl.col(f).str.to_lowercase() == _where_text(v).lower())
        index = table.group_by(lk.match).agg(
            pl.col(lk.result).first().alias("_taken"), pl.len().alias("_n")
        )
        joined = df.select(ROW, pl.col(name).alias("_value")).join(
            index, left_on="_value", right_on=lk.match, how="left"
        )
        among = " among " + ", ".join(f"{f} '{v}'" for f, v in lk.where.items()) if lk.where else ""
        missing = joined.filter(pl.col("_value").is_not_null() & pl.col("_n").is_null())
        several = joined.filter(pl.col("_n") > 1)
        errors.append(
            missing.select(
                ROW,
                pl.lit(name).alias("field"),
                pl.col("_value").alias("value"),
                (
                    pl.lit(f"no {other.flow} record with {lk.match} '")
                    + pl.col("_value")
                    + pl.lit(f"'{among}")
                ).alias("reason"),
            )
        )
        errors.append(
            several.select(
                ROW,
                pl.lit(name).alias("field"),
                pl.col("_value").alias("value"),
                (
                    pl.col("_n").cast(pl.String)
                    + pl.lit(f" {other.flow} records have {lk.match} '")
                    + pl.col("_value")
                    + pl.lit("': can't tell which one")
                ).alias("reason"),
            )
        )
        taken = joined.select(
            ROW, pl.when(pl.col("_n") == 1).then(pl.col("_taken")).alias("_taken")
        )
        value, bad, reason = _typed(pl.col("_taken"), m)
        typed = taken.with_columns(value.alias("_typed"))
        if bad is not None:
            errors.append(
                taken.filter(bad).select(
                    ROW,
                    pl.lit(name).alias("field"),
                    pl.col("_taken").alias("value"),
                    pl.lit(f"{lk.result} from {other.flow}: {reason}").alias("reason"),
                )
            )
        df = (
            df.join(typed.select(ROW, "_typed"), on=ROW, how="left")
            .with_columns(pl.col("_typed").alias(name))
            .drop("_typed")
        )
    all_errors = pl.concat([e.cast(ERROR_SCHEMA) for e in errors]).sort(ROW)  # type: ignore[arg-type]
    return Mapped(df=df, errors=all_errors)


def run_order(flows: list[Path]) -> list[Path]:
    """The given flow files, each after the flows it looks up (among those given)."""
    paths = [p.resolve() for p in flows]
    deps: dict[Path, set[Path]] = {}
    for p in paths:
        try:
            spec = load_flow(p)
        except SpecError:
            deps[p] = set()  # the run itself reports the problem
            continue
        deps[p] = {d for d in dependencies(spec, p).values() if d in paths and d != p}
    order: list[Path] = []
    visiting: list[Path] = []

    def visit(p: Path) -> None:
        if p in order:
            return
        if p in visiting:
            cycle = " → ".join(x.name for x in [*visiting[visiting.index(p) :], p])
            raise LinkError(f"flows look each other up in a cycle: {cycle}")
        visiting.append(p)
        for d in sorted(deps[p]):
            visit(d)
        visiting.pop()
        order.append(p)

    for p in paths:
        visit(p)
    return order
