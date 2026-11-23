"""Write a draft flow file from mapping suggestions.

The draft is meant to be read and edited by a person, so it's written by hand
(PyYAML can't write comments): every field says where it came from and how sure
the match is, and anything uncertain is marked `REVIEW`. Fields with no matching
column are left commented out, so the draft is a valid flow file as soon as
the key column is found.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import yaml

from syncline.entities import Entity
from syncline.mapping.suggest import Suggestion, distinct_values


def q(value: object) -> str:
    """A YAML scalar, quoted only when needed."""
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=1000)
    return text.removesuffix("\n").removesuffix("\n...").strip()


def _field_line(s: Suggestion, entity: Entity) -> list[str]:
    f = entity.get(s.field)
    assert f is not None
    if s.column is None:
        return [f"  # {s.field}: {{ from: ??? }}  # REVIEW: no matching column found"]
    parts = [f"from: {q(s.column)}"]
    if f.type != "string":
        parts.append(f"type: {f.type}")
    if s.date_format:
        parts.append(f"format: {q(s.date_format)}")
    if f.transform:
        parts.append(f"transform: [{', '.join(f.transform)}]")
    comment = f"{s.confidence:.2f} {s.reason}"
    if s.needs_review:
        comment = f"REVIEW ({comment})" + (f": {s.note}" if s.note else "")
    return [f"  {s.field}: {{ {', '.join(parts)} }}  # {comment}"]


def draft_flow(
    *,
    flow: str,
    entity: Entity,
    suggestions: list[Suggestion],
    sample: pl.DataFrame,
    source_path: str,
) -> str:
    mapped = {s.field for s in suggestions if s.column is not None}
    used = {s.column for s in suggestions if s.column is not None}
    unused = [c for c in sample.columns if c not in used]

    lines = [
        f"# Draft written by `syncline map` from {Path(source_path).name}.",
        "# Check every line marked REVIEW, then run: syncline run <this file> --dry-run",
        f"flow: {flow}",
        f"entity: {entity.name}",
        "source:",
        "  type: file",
        f"  path: {q(source_path)}",
        "mapping:",
    ]
    for s in suggestions:
        lines += _field_line(s, entity)
        f = entity.get(s.field)
        if f is not None and f.describe_values and s.column is not None:
            summary = distinct_values(sample, s.column)
            if summary:
                seen = ", ".join(f"{v} ({n})" for v, n in summary.values)
                lines.append(f"  #   values seen: {seen}")
                if summary.case_variants:
                    lines.append(
                        "  #   REVIEW: the same values appear in different capitalization; "
                        "add `lower` or `upper` to transform"
                    )
                lines.append("  #   add `values: {old: new}` to rename them")
    lines.append("  status: { default: active }  # every person in the file is active")
    if unused:
        lines.append(f"  # unused columns: {', '.join(unused)}")

    required = [f.name for f in entity.fields if f.required and f.name in mapped]
    unique = [f.name for f in entity.fields if f.unique and f.name in mapped]
    lines += ["contract:", f"  required: [{', '.join(required)}]"]
    if unique:
        lines.append(f"  unique: [{', '.join(unique)}]")
    lines += [
        "  max_rejected_ratio: 0.05",
        "load:",
        f'  target: {{ type: sql, url: "env:APP_DB_URL", table: {entity.table} }}',
        f"  key: [{entity.key}]",
        "  mode: full_snapshot  # people missing from the file are deactivated",
        "  deactivate: { set: { status: inactive } }",
    ]
    if entity.key not in mapped:
        lines.insert(
            2, f"# REVIEW: the key field '{entity.key}' has no column; the flow won't run."
        )
    return "\n".join(lines) + "\n"
