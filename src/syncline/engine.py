"""One run of a flow, stage by stage: read → map → check → diff → load → record."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncline.contract import Checked, ContractFailure, check
from syncline.diff import Diff, compute
from syncline.io.read import ROW, ReadResult, find_file, read_file
from syncline.mapping.apply import apply_mapping
from syncline.spec import FlowSpec, load_flow
from syncline.state import RunRecord, State, now
from syncline.targets.sql import SqlTable


@dataclass
class RunResult:
    record: RunRecord
    spec: FlowSpec
    read: ReadResult | None = None
    checked: Checked | None = None
    diff: Diff | None = None
    rejected: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.record.status


def new_run_id() -> str:
    return now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def _rejected_details(read: ReadResult, checked: Checked) -> list[dict[str, Any]]:
    """Each rejected row with its reasons and its values as they were in the file."""
    if checked.rejected.is_empty():
        return []
    joined = checked.rejected.join(read.df, on=ROW, how="left").sort(ROW)
    cols = read.columns
    return [
        {"row": r[ROW], "reasons": r["reasons"], "values": {c: r[c] for c in cols}}
        for r in joined.iter_rows(named=True)
    ]


def run(
    flow_path: Path,
    *,
    file: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    state: State | None = None,
) -> RunResult:
    """Run a flow. Contract failures and skips are results, not exceptions.

    Config, source and target problems raise (SpecError, SourceError, MappingError,
    TargetError): nothing has been written when they do.
    """
    state = state or State()
    spec = load_flow(flow_path)
    started = now()
    run_id = new_run_id()

    def record(status: str, **kw: Any) -> RunRecord:
        base: dict[str, Any] = dict(
            id=run_id, flow=spec.flow, file=None, sha256=None, started_at=started,
            finished_at=now(), status=status, total=0, created=0, updated=0,
            deactivated=0, unchanged=0, rejected=0, message=None,
        )  # fmt: skip
        base.update(kw)
        return RunRecord(**base)

    path = find_file(spec.source, flow_path.parent, file)
    read = read_file(path, spec.source)
    where = {"file": str(path), "sha256": read.sha256, "total": read.df.height}

    last = state.last_ok(spec.flow)
    if not force and not dry_run and last is not None and last.sha256 == read.sha256:
        rec = record("skipped", **where, message=f"same file as run {last.id}: nothing to do")
        state.save(rec)
        return RunResult(record=rec, spec=spec, read=read)

    mapped = apply_mapping(read.df, spec.mapping)
    try:
        checked = check(mapped, spec.contract)
    except ContractFailure as e:
        rejected = _rejected_details(read, e.checked)
        rec = record("rejected", **where, rejected=e.checked.rejected.height, message=str(e))
        state.save(rec, rejected)
        return RunResult(record=rec, spec=spec, read=read, checked=e.checked, rejected=rejected)

    deactivate = spec.load.deactivate.set if spec.load.deactivate else None
    table = SqlTable.open(
        spec.load.target, spec.mapping, spec.load.key, extra=deactivate, create=not dry_run
    )
    current = table.read(spec.load.key)
    diff = compute(checked.valid, current, spec.load.key, deactivate, present=mapped.df)

    if not dry_run:
        table.apply(diff)

    rejected = _rejected_details(read, checked)
    c = diff.counts()
    rec = record(
        "dry_run" if dry_run else "ok",
        **where,
        created=c["create"],
        updated=c["update"],
        deactivated=c["deactivate"],
        unchanged=c["unchanged"],
        rejected=checked.rejected.height,
    )
    state.save(rec, rejected)
    return RunResult(
        record=rec, spec=spec, read=read, checked=checked, diff=diff, rejected=rejected
    )


def preview_changes(diff: Diff, limit: int = 5) -> list[str]:
    """A few human-readable lines per change type, for the terminal summary."""
    lines: list[str] = []
    key = diff.key
    for row in diff.update.head(limit).iter_rows(named=True):
        changes = ", ".join(f"{f}: {row[f'old_{f}']} → {row[f]}" for f in row["__changed"])
        lines.append(f"~ {'/'.join(str(row[k]) for k in key)}  {changes}")
    for row in diff.create.head(limit).iter_rows(named=True):
        lines.append(f"+ {'/'.join(str(row[k]) for k in key)}")
    for row in diff.deactivate.head(limit).iter_rows(named=True):
        lines.append(f"- {'/'.join(str(row[k]) for k in key)}")
    return lines
