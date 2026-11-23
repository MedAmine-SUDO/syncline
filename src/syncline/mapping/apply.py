"""Apply a flow's mapping: source columns in, typed target fields out.

A value that can't be converted (a date that doesn't exist, an e-mail without an @)
never stops the run. It becomes a cell error with a reason, and the contract check
decides what happens to the row.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from syncline.io.read import ROW
from syncline.spec import FieldMapping

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s.]+$"
_TRUE = ["true", "1", "yes", "y", "oui", "o", "vrai"]
_FALSE = ["false", "0", "no", "n", "non", "faux"]
_FORMAT_HINT = {"%d/%m/%Y": "dd/mm/yyyy", "%Y-%m-%d": "yyyy-mm-dd", "%m/%d/%Y": "mm/dd/yyyy"}


class MappingError(Exception):
    """The file doesn't fit the mapping at all (e.g. a mapped column is missing)."""


@dataclass(frozen=True)
class Mapped:
    df: pl.DataFrame  # ROW + one typed column per target field
    errors: pl.DataFrame  # ROW, field, value, reason: one row per bad cell


ERROR_SCHEMA = {ROW: pl.UInt32, "field": pl.String, "value": pl.String, "reason": pl.String}


def _transform(expr: pl.Expr, name: str) -> pl.Expr:
    match name:
        case "trim":
            return expr.str.strip_chars()
        case "lower":
            return expr.str.to_lowercase()
        case "upper":
            return expr.str.to_uppercase()
        case "title":
            return expr.str.to_titlecase()
        case "collapse_spaces":
            return expr.str.replace_all(r"\s+", " ").str.strip_chars()
        case "digits_only":
            return expr.str.replace_all(r"\D", "")
    raise ValueError(f"unknown transform {name}")  # unreachable: validated by the spec


def _prepared(m: FieldMapping) -> pl.Expr:
    """Text value after transforms, value map and default; before typing."""
    expr = pl.col(m.source) if m.source is not None else pl.lit(None, dtype=pl.String)
    for t in m.transform:
        expr = _transform(expr, t)
    expr = pl.when(expr == "").then(None).otherwise(expr)
    if m.values:
        expr = expr.replace({k: str(v) for k, v in m.values.items()})
    if m.default is not None:
        expr = expr.fill_null(str(m.default))
    return expr


def _typed(text: pl.Expr, m: FieldMapping) -> tuple[pl.Expr, pl.Expr | None, str]:
    """(typed value, condition marking a bad cell, reason)."""
    match m.type:
        case "string":
            return text, None, ""
        case "email":
            bad = text.is_not_null() & ~text.str.contains(EMAIL_RE)
            return text, bad, "not a valid e-mail address"
        case "int":
            value = text.str.replace_all(r"\s", "").str.to_integer(strict=False)
            return value, text.is_not_null() & value.is_null(), "not a whole number"
        case "float":
            cleaned = text.str.replace_all(r"\s", "").str.replace(",", ".", literal=True)
            value = cleaned.cast(pl.Float64, strict=False)
            return value, text.is_not_null() & value.is_null(), "not a number"
        case "bool":
            low = text.str.to_lowercase()
            value = (
                pl.when(low.is_in(_TRUE))
                .then(True)
                .when(low.is_in(_FALSE))
                .then(False)
                .otherwise(None)
            )
            return value, text.is_not_null() & value.is_null(), "not yes/no"
        case "date":
            fmt = m.format or "%Y-%m-%d"
            value = text.str.strptime(pl.Date, fmt, strict=False)
            hint = _FORMAT_HINT.get(fmt, fmt)
            return value, text.is_not_null() & value.is_null(), f"not a valid date ({hint})"
    raise ValueError(f"unknown type {m.type}")  # unreachable


def missing_columns(mapping: dict[str, FieldMapping], columns: list[str]) -> list[str]:
    return [m.source for m in mapping.values() if m.source is not None and m.source not in columns]


def apply_mapping(df: pl.DataFrame, mapping: dict[str, FieldMapping]) -> Mapped:
    columns = [c for c in df.columns if c != ROW]
    missing = missing_columns(mapping, columns)
    if missing:
        raise MappingError(
            f"column(s) not in the file: {', '.join(missing)}. The file has: {', '.join(columns)}"
        )

    text_cols, typed_cols, error_frames = [], [], []
    for field, m in mapping.items():
        text_cols.append(_prepared(m).alias(field))
    text_df = df.select(pl.col(ROW), *text_cols)

    for field, m in mapping.items():
        value, bad, reason = _typed(pl.col(field), m)
        typed_cols.append(value.alias(field))
        if bad is not None:
            error_frames.append(
                text_df.filter(bad).select(
                    pl.col(ROW),
                    pl.lit(field).alias("field"),
                    pl.col(field).alias("value"),
                    pl.lit(reason).alias("reason"),
                )
            )

    out = text_df.select(pl.col(ROW), *typed_cols)
    errors = (
        pl.concat(error_frames).cast(ERROR_SCHEMA)  # type: ignore[arg-type]
        if error_frames
        else pl.DataFrame(schema=ERROR_SCHEMA)
    )
    return Mapped(df=out, errors=errors.sort(ROW))
