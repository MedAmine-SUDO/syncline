"""Compare what the file says with what the target holds, before writing anything.

The diff drives everything after it: the dry-run preview, the load and the report all
use the same frames, so what you preview is exactly what gets applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from syncline.io.read import ROW
from syncline.spec import Scalar

CHANGED = "__changed"  # list of field names that differ, on update rows


@dataclass(frozen=True)
class Diff:
    key: list[str]
    create: pl.DataFrame  # incoming rows whose key isn't in the target
    update: pl.DataFrame  # incoming rows that differ from the target (+ CHANGED, + old_<f>)
    deactivate: pl.DataFrame  # target keys absent from the file (full snapshot only)
    unchanged: int
    deactivate_set: dict[str, Scalar] = field(default_factory=dict)
    already_inactive: int = 0  # target records absent from the file and already deactivated

    @property
    def is_empty(self) -> bool:
        return self.create.is_empty() and self.update.is_empty() and self.deactivate.is_empty()

    def counts(self) -> dict[str, int]:
        return {
            "create": self.create.height,
            "update": self.update.height,
            "deactivate": self.deactivate.height,
            "unchanged": self.unchanged,
        }


def compute(
    incoming: pl.DataFrame,
    current: pl.DataFrame,
    key: list[str],
    deactivate_set: dict[str, Scalar] | None = None,
    present: pl.DataFrame | None = None,
) -> Diff:
    """incoming: valid rows (ROW + fields). current: target rows (key + same fields).

    present: every key found in the file, rejected rows included. A record whose row
    was rejected is still in the file, so it is never deactivated: a bad row must not
    switch off a real person.
    """
    fields = [c for c in incoming.columns if c not in (ROW, *key)]
    old = current.select(
        *key, *[pl.col(f).alias(f"old_{f}") for f in fields if f in current.columns]
    )
    missing_in_target = [f for f in fields if f not in current.columns]
    if missing_in_target:
        old = old.with_columns(pl.lit(None).alias(f"old_{f}") for f in missing_in_target)

    create = incoming.join(current.select(key), on=key, how="anti")
    matched = incoming.join(old, on=key, how="inner")
    if fields:
        changed_flags = [pl.col(f).ne_missing(pl.col(f"old_{f}")) for f in fields]
        matched = matched.with_columns(
            pl.concat_list(
                [
                    pl.when(flag).then(pl.lit(f))
                    for f, flag in zip(fields, changed_flags, strict=True)
                ]
            )
            .list.drop_nulls()
            .alias(CHANGED)
        )
    else:
        matched = matched.with_columns(pl.lit([], dtype=pl.List(pl.String)).alias(CHANGED))
    update = matched.filter(pl.col(CHANGED).list.len() > 0)
    unchanged = matched.height - update.height

    if deactivate_set:
        seen = incoming.select(key)
        if present is not None:
            seen = pl.concat([seen, present.select(key).drop_nulls().cast(seen.schema)]).unique()
        gone = current.join(seen, on=key, how="anti")
        already = pl.all_horizontal(
            [
                pl.col(f).cast(pl.String).eq_missing(pl.lit(str(v)))
                for f, v in deactivate_set.items()
            ]
        )
        deactivate = gone.filter(~already).select(key)
        already_inactive = gone.height - deactivate.height
        unchanged += already_inactive
    else:
        deactivate = current.select(key).clear()
        already_inactive = 0

    return Diff(
        key=key,
        create=create.sort(ROW),
        update=update.sort(ROW),
        deactivate=deactivate.sort(key),
        unchanged=unchanged,
        deactivate_set=dict(deactivate_set or {}),
        already_inactive=already_inactive,
    )
