"""One run of a flow, stage by stage: read → map → check → diff → guard → load → record.

Every run ends with exactly one record in the run history, whatever happened:

| status     | meaning                                                         | target   |
|------------|-----------------------------------------------------------------|----------|
| `ok`       | changes applied                                                 | written  |
| `dry_run`  | changes computed and recorded as a preview                      | untouched|
| `skipped`  | same file as the last successful run                            | untouched|
| `rejected` | too many invalid rows (contract)                                | untouched|
| `guarded`  | changes too large (mass-change guard)                           | untouched|

Problems with the flow file, the source or the target raise exceptions instead
(SpecError, SourceError, MappingError, TargetError); nothing is written when they do.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncline.contract import Checked, ContractFailure, check
from syncline.diff import CHANGED, Diff, compute
from syncline.io.read import ROW, ReadResult, find_file, read_file
from syncline.mapping.apply import apply_mapping
from syncline.spec import FlowSpec, Guard, load_flow
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
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.record.status


def new_run_id() -> str:
    return now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def rejected_details(read: ReadResult, checked: Checked) -> list[dict[str, Any]]:
    """Each rejected row with its reasons and its values as they were in the file."""
    if checked.rejected.is_empty():
        return []
    joined = checked.rejected.join(read.df, on=ROW, how="left").sort(ROW)
    cols = read.columns
    return [
        {"row": r[ROW], "reasons": r["reasons"], "values": {c: r[c] for c in cols}}
        for r in joined.iter_rows(named=True)
    ]


def change_log(diff: Diff) -> list[dict[str, Any]]:
    """One audit entry per changed record: what was created, and what changed from what."""
    key = diff.key
    log: list[dict[str, Any]] = []
    for row in diff.create.drop(ROW).iter_rows(named=True):
        detail = {f: v for f, v in row.items() if f not in key}
        log.append({"action": "create", "key": {k: row[k] for k in key}, "detail": detail})
    for row in diff.update.iter_rows(named=True):
        detail = {f: [row[f"old_{f}"], row[f]] for f in row[CHANGED]}
        log.append({"action": "update", "key": {k: row[k] for k in key}, "detail": detail})
    for row in diff.deactivate.iter_rows(named=True):
        detail = dict(diff.deactivate_set)
        log.append({"action": "deactivate", "key": {k: row[k] for k in key}, "detail": detail})
    return log


def guard_violations(diff: Diff, existing: int, guard: Guard) -> list[str]:
    """Reasons the changes look too large, relative to the active records in the target.

    Records that are already deactivated don't count: a file can't remove them twice.
    """
    existing -= diff.already_inactive
    if existing <= 0:
        return []  # first load: everything is a create
    problems = []
    c = diff.counts()
    ratio = c["deactivate"] / existing
    if ratio > guard.max_deactivate_ratio:
        problems.append(
            f"would deactivate {c['deactivate']} of {existing} records ({ratio:.0%}), "
            f"above the limit of {guard.max_deactivate_ratio:.0%}"
        )
    if guard.max_update_ratio is not None:
        ratio = c["update"] / existing
        if ratio > guard.max_update_ratio:
            problems.append(
                f"would update {c['update']} of {existing} records ({ratio:.0%}), "
                f"above the limit of {guard.max_update_ratio:.0%}"
            )
    return problems


def run(
    flow_path: Path,
    *,
    file: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    allow_mass_change: bool = False,
    state: State | None = None,
) -> RunResult:
    """Run a flow and record the run. See the module docstring for the outcomes."""
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
        rejected = rejected_details(read, e.checked)
        rec = record("rejected", **where, rejected=e.checked.rejected.height, message=str(e))
        state.save(rec, rejected)
        return RunResult(record=rec, spec=spec, read=read, checked=e.checked, rejected=rejected)

    deactivate = spec.load.deactivate.set if spec.load.deactivate else None
    table = SqlTable.open(
        spec.load.target, spec.mapping, spec.load.key, extra=deactivate, create=not dry_run
    )
    current = table.read(spec.load.key)
    policies = {f: m.update for f, m in spec.mapping.items()}
    diff = compute(
        checked.valid, current, spec.load.key, deactivate, present=mapped.df, policies=policies
    )
    rejected = rejected_details(read, checked)
    counts = diff.counts()
    totals = dict(
        created=counts["create"],
        updated=counts["update"],
        deactivated=counts["deactivate"],
        unchanged=counts["unchanged"],
        rejected=checked.rejected.height,
    )
    result = RunResult(
        record=record("pending"), spec=spec, read=read, checked=checked, diff=diff,
        rejected=rejected,
    )  # fmt: skip

    violations = guard_violations(diff, current.height, spec.load.guard)
    if violations and not allow_mass_change:
        message = "; ".join(violations)
        if dry_run:
            result.warnings.append(f"a real run would stop: {message}")
        else:
            result.record = record("guarded", **where, **totals, message=message)
            state.save(result.record, rejected, change_log(diff))
            return result

    if not dry_run:
        table.apply(diff)

    note = "; ".join(result.warnings) or None
    result.record = record("dry_run" if dry_run else "ok", **where, **totals, message=note)
    state.save(result.record, rejected, change_log(diff))
    return result


def preview_changes(diff: Diff, limit: int = 5) -> list[str]:
    """A few human-readable lines per change type, for the terminal summary."""
    lines: list[str] = []
    key = diff.key
    for row in diff.update.head(limit).iter_rows(named=True):
        changes = ", ".join(f"{f}: {row[f'old_{f}']} → {row[f]}" for f in row[CHANGED])
        lines.append(f"~ {'/'.join(str(row[k]) for k in key)}  {changes}")
    for row in diff.create.head(limit).iter_rows(named=True):
        lines.append(f"+ {'/'.join(str(row[k]) for k in key)}")
    for row in diff.deactivate.head(limit).iter_rows(named=True):
        lines.append(f"- {'/'.join(str(row[k]) for k in key)}")
    return lines
