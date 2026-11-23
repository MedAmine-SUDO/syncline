"""One run of a flow, stage by stage: read → map → check → diff → guard → load → record.

Every run ends with exactly one record in the run history, whatever happened:

| status     | meaning                                                         | target   |
|------------|-----------------------------------------------------------------|----------|
| `ok`       | changes applied                                                 | written  |
| `dry_run`  | changes computed and recorded as a preview                      | untouched|
| `skipped`  | same file as the last successful run                            | untouched|
| `rejected` | too many invalid rows (contract)                                | untouched|
| `guarded`  | changes too large (mass-change guard)                           | untouched|
| `partial`  | some writes refused by an API target (each one recorded)        | partly   |

Problems with the flow file, the source, a hook or the target raise exceptions instead
(SpecError, SourceError, MappingError, HookError, LinkError, TargetError); nothing is written
when they do.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from syncline.archive import Archive
from syncline.contract import Checked, ContractFailure, check
from syncline.diff import CHANGED, Diff, compute
from syncline.hooks import HookContext, Hooks, Stage
from syncline.io.fetch import fetch
from syncline.io.read import ROW, ReadResult, read_file
from syncline.lifecycle import enforce
from syncline.lookups import resolve
from syncline.mapping.apply import Mapped, apply_mapping
from syncline.spec import FlowSpec, Guard, load_flow
from syncline.state import RunRecord, State, now
from syncline.targets.base import ApplyResult, open_target


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
    archive: Archive | None = None,
    replay_of: str | None = None,
) -> RunResult:
    """Run a flow and record the run. See the module docstring for the outcomes.

    With an `archive`, the input file is kept (by content hash) before anything else
    happens, so the run can be replayed later on exactly the same bytes.
    """
    state = state or State()
    spec = load_flow(flow_path)
    started = now()
    run_id = new_run_id()

    def record(status: str, **kw: Any) -> RunRecord:
        base: dict[str, Any] = dict(
            id=run_id, flow=spec.flow, file=None, sha256=None, started_at=started,
            finished_at=now(), status=status, total=0, created=0, updated=0,
            deactivated=0, unchanged=0, rejected=0, failed=0, message=None,
            flow_path=str(flow_path.resolve()),
        )  # fmt: skip
        base.update(kw)
        if replay_of:
            note = f"replay of run {replay_of}"
            base["message"] = f"{note}; {base['message']}" if base["message"] else note
        return RunRecord(**base)

    hooks = Hooks.load(spec.hooks.configured(), flow_path.parent)
    # Remote files are downloaded here; the folder is removed when the run ends.
    with tempfile.TemporaryDirectory(prefix="syncline-") as tmp:
        workdir = Path(tmp)
        fetched = fetch(spec.source, flow_path.parent, workdir, file)
        path = fetched.path
        read = read_file(path, spec.source)
        kept = archive.store(spec.flow, path, read.sha256) if archive else None
        where = {
            "file": fetched.origin, "sha256": read.sha256, "total": read.df.height,
            "archived": str(kept) if kept else None,
        }  # fmt: skip

        last = state.last_ok(spec.flow)
        if not force and not dry_run and last is not None and last.sha256 == read.sha256:
            rec = record("skipped", **where, message=f"same file as run {last.id}: nothing to do")
            state.save(rec)
            return RunResult(record=rec, spec=spec, read=read)

        def ctx(stage: Stage) -> HookContext:
            return HookContext(spec.flow, spec.entity, path, run_id, dry_run, stage, fetched.origin)

        text = hooks.run("after_read", read.df, ctx("after_read"))
        mapped = apply_mapping(text, spec.mapping)
        mapped = resolve(mapped, spec, flow_path, run_id)
        after_map = hooks.run("after_map", mapped.df, ctx("after_map"))
        mapped = Mapped(df=after_map, errors=mapped.errors)
        try:
            checked = check(mapped, spec.contract)
        except ContractFailure as e:
            rejected = rejected_details(read, e.checked)
            rec = record("rejected", **where, rejected=e.checked.rejected.height, message=str(e))
            state.save(rec, rejected)
            return RunResult(record=rec, spec=spec, read=read, checked=e.checked, rejected=rejected)

        deactivate = spec.load.deactivate.set if spec.load.deactivate else None
        table = open_target(
            spec.load.target, spec.mapping, spec.load.key, extra=deactivate,
            create=not dry_run, run_id=run_id,
        )  # fmt: skip
        current = table.read(spec.load.key)
        policies = {f: m.update for f, m in spec.mapping.items()}
        diff = compute(
            checked.valid, current, spec.load.key, deactivate, present=mapped.df, policies=policies
        )
        if spec.lifecycle:
            enforced = enforce(diff, current, spec.lifecycle)
            diff = enforced.diff
            result_warnings = enforced.warnings
            if not enforced.rejected.is_empty():
                checked = Checked(
                    valid=checked.valid.join(enforced.rejected.select(ROW), on=ROW, how="anti"),
                    rejected=pl.concat(
                        [checked.rejected, enforced.rejected.cast(checked.rejected.schema)]
                    ).sort(ROW),
                    total=checked.total,
                )
                if checked.rejected_ratio > spec.contract.max_rejected_ratio:
                    failure = ContractFailure(checked, spec.contract.max_rejected_ratio)
                    rejected = rejected_details(read, checked)
                    rec = record(
                        "rejected", **where, rejected=checked.rejected.height, message=str(failure)
                    )
                    state.save(rec, rejected)
                    return RunResult(
                        record=rec, spec=spec, read=read, checked=checked, rejected=rejected
                    )
        else:
            result_warnings = []
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
            rejected=rejected, warnings=list(result_warnings),
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

        log = change_log(diff)
        status = "dry_run" if dry_run else "ok"
        if not dry_run:
            outcome = table.apply(diff)
            if outcome.failed:
                status = "partial"
                log = applied_log(log, outcome)
                totals["created"] -= outcome.failed_count("create")
                totals["updated"] -= outcome.failed_count("update")
                totals["deactivated"] -= outcome.failed_count("deactivate")
                totals["failed"] = len(outcome.failed)
                result.warnings.insert(0, partial_message(outcome))

        note = "; ".join(result.warnings) or None
        result.record = record(status, **where, **totals, message=note)
        state.save(result.record, rejected, log)
        return result


def applied_log(log: list[dict[str, Any]], outcome: ApplyResult) -> list[dict[str, Any]]:
    """The change log of what was really applied, plus one `failed` entry per refused write."""
    failed = {(f.action, json.dumps(f.key, default=str, sort_keys=True)) for f in outcome.failed}
    kept = [
        e for e in log
        if (e["action"], json.dumps(e["key"], default=str, sort_keys=True)) not in failed
    ]  # fmt: skip
    return kept + [
        {"action": "failed", "key": f.key,
         "detail": {"action": f.action, "status": f.status, "error": f.error}}
        for f in outcome.failed
    ]  # fmt: skip


def partial_message(outcome: ApplyResult) -> str:
    refused = sum(1 for f in outcome.failed if f.status is not None)
    not_sent = sum(1 for f in outcome.failed if f.error.startswith("not sent"))
    parts = [f"{refused} write(s) refused by the target"] if refused else []
    if outcome.stopped:
        parts.append(f"stopped: {outcome.stopped}; {not_sent} not sent")
    parts.append("the next run retries them (the diff is computed from the target again)")
    return "; ".join(parts)


class ReplayError(Exception):
    """A run can't be replayed (unknown run, no archived file, wrong flow file)."""


def replay(
    run_id: str,
    *,
    state: State,
    flow_path: Path | None = None,
    dry_run: bool = False,
    allow_mass_change: bool = False,
) -> RunResult:
    """Run a past run's archived input file again, through the flow's current configuration.

    Useful after fixing a mapping or a contract: the same bytes, the new rules. The archived
    file is checked against its hash first; the replay is recorded as a new run.
    """
    past = state.find(run_id)
    if past is None:
        raise ReplayError(f"no single run matches {run_id!r} (see `syncline runs`)")
    if not past.archived or not past.sha256:
        raise ReplayError(f"run {past.id} has no archived input file (it ran with --no-archive)")
    flow = flow_path or (Path(past.flow_path) if past.flow_path else None)
    if flow is None:
        raise ReplayError(f"run {past.id} doesn't record its flow file; pass --flow")
    spec = load_flow(flow)
    if spec.flow != past.flow:
        raise ReplayError(f"{flow} is flow {spec.flow!r}, but run {past.id} was {past.flow!r}")
    kept = Archive.verify(Path(past.archived), past.sha256)
    return run(
        flow, file=kept, dry_run=dry_run, force=True, allow_mass_change=allow_mass_change,
        state=state, replay_of=past.id,
    )  # fmt: skip


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
