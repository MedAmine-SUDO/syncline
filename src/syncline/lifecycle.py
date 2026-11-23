"""Lifecycle rules: a status-like field may only move along the transitions the flow allows.

Checked on the diff, because a transition needs both values: the one in the target and the
one in the file. A row that asks for a forbidden change (an `inactive` employee coming back
as `pending`, a `cancelled` training reopened) is **rejected as a whole**, with a reason and
its line number, like a contract failure: none of its other changes are written either,
since the row is likely wrong as a whole. New records must start in an `initial` state.

Deactivations follow the rules too. A record the lifecycle doesn't allow to be deactivated
from its current state is left alone, and the run says so.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import polars as pl

from syncline.diff import CHANGED, Diff
from syncline.io.read import ROW
from syncline.spec import Lifecycle

_REJECTED_SCHEMA: dict[str, pl.DataType] = {ROW: pl.UInt32(), "reasons": pl.List(pl.String)}


@dataclass(frozen=True)
class Enforced:
    diff: Diff
    rejected: pl.DataFrame  # ROW, reasons: rows whose change the lifecycle forbids
    kept: list[dict[str, Any]]  # keys of records not deactivated (forbidden from their state)
    warnings: list[str]


def _quote(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values) or "none"


def _check_new(lc: Lifecycle, value: Any) -> str | None:
    if value is None:
        return None
    v = str(value)
    if v not in lc.states():
        return f"unknown {lc.field} '{v}' (states: {_quote(lc.states())})"
    if lc.initial is not None and v not in [str(i) for i in lc.initial]:
        initial = [str(i) for i in lc.initial]
        return f"a new record can't start as {lc.field} '{v}' (allowed: {_quote(initial)})"
    return None


def _check_move(lc: Lifecycle, old: Any, new: Any) -> str | None:
    if old is None:
        return _check_new(lc, new)
    o = str(old)
    if new is None:
        return f"{lc.field} can't be emptied (it is '{o}')"
    n = str(new)
    if n not in lc.states():
        return f"unknown {lc.field} '{n}' (states: {_quote(lc.states())})"
    if o not in lc.states():
        return f"the target's current {lc.field} '{o}' is not a lifecycle state; fix it there first"
    allowed = lc.allowed_from(o)
    if n not in allowed:
        return f"{lc.field} can't go from '{o}' to '{n}' (from '{o}': {_quote(allowed)})"
    return None


def enforce(diff: Diff, current: pl.DataFrame, lc: Lifecycle) -> Enforced:
    f = lc.field
    rejected: list[tuple[int, list[str]]] = []

    bad_create = [
        (row[ROW], reason)
        for row in diff.create.select(ROW, f).iter_rows(named=True)
        if (reason := _check_new(lc, row[f])) is not None
    ]
    moving = diff.update.filter(pl.col(CHANGED).list.contains(f))
    bad_update = [
        (row[ROW], reason)
        for row in moving.select(ROW, f"old_{f}", f).iter_rows(named=True)
        if (reason := _check_move(lc, row[f"old_{f}"], row[f])) is not None
    ]
    rejected = [(r, [reason]) for r, reason in bad_create + bad_update]
    rows = {r for r, _ in rejected}

    keep = pl.col(ROW).is_in(list(rows)).not_()
    new_diff = replace(diff, create=diff.create.filter(keep), update=diff.update.filter(keep))

    kept: list[dict[str, Any]] = []
    warnings: list[str] = []
    target_state = diff.deactivate_set.get(f)
    if target_state is not None and not diff.deactivate.is_empty():
        key = diff.key
        states = diff.deactivate.join(current.select(*key, f), on=key, how="left")
        blocked = [
            row for row in states.iter_rows(named=True)
            # an empty current state is not a forbidden move: it just isn't tracked yet
            if row[f] is not None and _check_move(lc, row[f], target_state) is not None
        ]  # fmt: skip
        if blocked:
            kept = [{k: row[k] for k in key} for row in blocked]
            blocked_keys = pl.DataFrame(kept, schema={k: diff.deactivate.schema[k] for k in key})
            new_diff = replace(
                new_diff,
                deactivate=new_diff.deactivate.join(blocked_keys, on=key, how="anti"),
                unchanged=new_diff.unchanged + len(blocked),
            )
            by_state: dict[str, int] = {}
            for row in blocked:
                by_state[str(row[f])] = by_state.get(str(row[f]), 0) + 1
            detail = ", ".join(f"{n} {state}" for state, n in sorted(by_state.items()))
            sample = ", ".join("/".join(str(v) for v in k.values()) for k in kept[:5])
            more = f" and {len(kept) - 5} more" if len(kept) > 5 else ""
            warnings.append(
                f"{len(kept)} record(s) missing from the file were not deactivated: the lifecycle "
                f"doesn't allow {f} '{target_state}' from their state ({detail}): {sample}{more}"
            )

    frame = pl.DataFrame(
        {ROW: [r for r, _ in rejected], "reasons": [rs for _, rs in rejected]},
        schema=_REJECTED_SCHEMA,
    ).sort(ROW)
    return Enforced(diff=new_diff, rejected=frame, kept=kept, warnings=warnings)
