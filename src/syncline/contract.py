"""The contract check: which rows may be loaded, and why the others may not.

A row is rejected if any of its cells failed to convert, a required field is empty,
a value isn't in the allowed list, or a unique field appears more than once.
Duplicates reject every copy: the engine can't know which one is right,
and loading either would be a guess.

If too many rows are rejected (`max_rejected_ratio`), the whole run stops:
a file that's mostly wrong usually means the wrong file, not bad rows.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from syncline.io.read import ROW
from syncline.mapping.apply import ERROR_SCHEMA, Mapped
from syncline.spec import Contract


@dataclass(frozen=True)
class Checked:
    valid: pl.DataFrame  # ROW + target fields, loadable
    rejected: pl.DataFrame  # ROW, reasons (list[str]): one row per rejected source row
    total: int

    @property
    def rejected_ratio(self) -> float:
        return self.rejected.height / self.total if self.total else 0.0


class ContractFailure(Exception):
    """Too many rejected rows: nothing is loaded."""

    def __init__(self, checked: Checked, limit: float) -> None:
        self.checked = checked
        self.limit = limit
        super().__init__(
            f"{checked.rejected.height} of {checked.total} rows rejected "
            f"({checked.rejected_ratio:.1%}), above the limit of {limit:.0%}"
        )


def _issues(df: pl.DataFrame, contract: Contract) -> list[pl.DataFrame]:
    frames = []

    for field in contract.required:
        frames.append(
            df.filter(pl.col(field).is_null()).select(
                pl.col(ROW),
                pl.lit(field).alias("field"),
                pl.lit(None, dtype=pl.String).alias("value"),
                pl.lit(f"missing {field}").alias("reason"),
            )
        )

    for field, allowed in contract.allowed.items():
        values = [str(a) for a in allowed]
        text = pl.col(field).cast(pl.String)
        frames.append(
            df.filter(text.is_not_null() & ~text.is_in(values)).select(
                pl.col(ROW),
                pl.lit(field).alias("field"),
                text.alias("value"),
                (
                    pl.lit(f"{field} '") + text + pl.lit(f"' is not one of: {', '.join(values)}")
                ).alias("reason"),
            )
        )

    for field in contract.unique:
        text = pl.col(field).cast(pl.String)
        dupes = (
            df.filter(pl.col(field).is_not_null())
            .with_columns(
                pl.len().over(field).alias("_n"),
                pl.col(ROW).cast(pl.String).implode().over(field).alias("_rows"),
            )
            .filter(pl.col("_n") > 1)
        )
        frames.append(
            dupes.select(
                pl.col(ROW),
                pl.lit(field).alias("field"),
                text.alias("value"),
                (
                    pl.lit(f"duplicate {field} '")
                    + text
                    + pl.lit("' (rows ")
                    + pl.col("_rows").list.join(", ")
                    + pl.lit(")")
                ).alias("reason"),
            )
        )
    return frames


def check(mapped: Mapped, contract: Contract) -> Checked:
    """Split rows into loadable and rejected. Raises ContractFailure above the limit."""
    df = mapped.df
    issues = pl.concat(
        [mapped.errors.cast(ERROR_SCHEMA), *(f.cast(ERROR_SCHEMA) for f in _issues(df, contract))]  # type: ignore[arg-type]
    )
    rejected = (
        issues.group_by(ROW, maintain_order=True)
        .agg(pl.col("reason").unique(maintain_order=True).alias("reasons"))
        .sort(ROW)
    )
    valid = df.join(rejected.select(ROW), on=ROW, how="anti")
    checked = Checked(valid=valid, rejected=rejected, total=df.height)
    if checked.rejected_ratio > contract.max_rejected_ratio:
        raise ContractFailure(checked, contract.max_rejected_ratio)
    return checked
