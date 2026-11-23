"""Command-line interface."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from syncline import __version__
from syncline.archive import Archive, ArchiveError
from syncline.engine import ReplayError, RunResult, preview_changes
from syncline.engine import replay as replay_run
from syncline.engine import run as run_flow
from syncline.entities import ENTITIES, Entity, custom_entity
from syncline.hooks import HookError, Hooks
from syncline.io.read import SourceError, read_file
from syncline.lookups import LinkError, dependencies, load_linked, run_order
from syncline.mapping.apply import MappingError
from syncline.mapping.draft import draft_flow
from syncline.mapping.suggest import REVIEW, SAMPLE_ROWS, OfflineMatcher
from syncline.report.html import ReportError, render_html, render_json, write_report
from syncline.spec import FileSource, SpecError, json_schema, load_flow
from syncline.state import State
from syncline.targets.sql import TargetError

app = typer.Typer(
    help="Describe a data sync in a YAML file instead of writing a script.",
    no_args_is_help=True,
    add_completion=False,
)
out = Console()
err = Console(stderr=True)

EXIT_CONFIG = 1  # flow file, source file or mapping problem
EXIT_CONTRACT = 2  # too many rejected rows: nothing loaded
EXIT_TARGET = 3  # target unreachable or doesn't match
EXIT_GUARD = 4  # changes too large: nothing written
EXIT_PARTIAL = 5  # an API target refused some writes: the rest were applied

StateOpt = Annotated[
    Path | None,
    typer.Option(
        "--state", help="Run history database [default: $SYNCLINE_STATE or ~/.syncline/state.db]."
    ),
]
ArchiveOpt = Annotated[
    Path | None,
    typer.Option(
        "--archive",
        help="Where input files are kept, for replay "
        "[default: $SYNCLINE_ARCHIVE or ~/.syncline/archive].",
    ),
]


def _version(value: bool) -> None:
    if value:
        out.print(f"syncline {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    """Syncline: messy customer files in, clean upserts out."""


@app.command()
def validate(flow: Annotated[Path, typer.Argument(help="Flow file (.yaml)")]) -> None:
    """Check a flow file without running it."""
    try:
        spec = load_flow(flow)
        Hooks.load(
            spec.hooks.configured(), flow.parent
        )  # hook files exist and their functions import
        for path in dependencies(spec, flow).values():
            load_linked(path, spec)  # looked-up flow files exist and are valid
    except Exception as e:
        _fail(e)
    out.print(
        f"[bold green]✓ {flow}[/] is valid: flow [bold]{spec.flow}[/], "
        f"{len(spec.mapping)} fields, key {', '.join(spec.load.key)}, "
        f"mode {spec.load.mode}"
    )


@app.command()
def schema() -> None:
    """Print the JSON Schema for flow files (for editor autocomplete)."""
    print(json.dumps(json_schema(), indent=2, ensure_ascii=False))


def _print_spec_error(e: SpecError) -> None:
    err.print(f"[bold red]✗ {e.path}[/] has {len(e.problems)} problem(s):")
    for p in e.problems:
        err.print(f"  • {p}")


def _summary(res: RunResult, max_rejected: int = 10) -> None:
    rec, read = res.record, res.read
    if read is not None:
        fmt = f"{read.encoding}, delimiter {read.delimiter!r}" if read.encoding else "Excel"
        out.print(f"[dim]file[/]  {read.path.name}  [dim]({fmt}, {read.df.height} rows)[/]")

    if rec.status == "skipped":
        out.print(f"[yellow]● skipped[/]: {rec.message}")
        return

    if res.diff is not None:
        verb = {"dry_run": "Would", "guarded": "Would", "partial": "Sent:"}.get(rec.status, "Done:")
        c = res.diff.counts()
        out.print(
            f"[bold]{verb}[/] create [green]{c['create']}[/], update [cyan]{c['update']}[/], "
            f"deactivate [magenta]{c['deactivate']}[/]  [dim](unchanged {c['unchanged']})[/]"
        )
        for line in preview_changes(res.diff):
            out.print(f"  [dim]{line}[/]")
        if res.diff.is_empty and rec.status == "ok":
            out.print("[green]Nothing to change: the target already matches the file.[/]")

    if res.rejected:
        out.print(f"[bold red]Rejected {len(res.rejected)} row(s)[/] (not loaded):")
        t = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
        t.add_column("row", justify="right")
        t.add_column("reason")
        for r in res.rejected[:max_rejected]:
            t.add_row(str(r["row"]), "; ".join(r["reasons"]))
        out.print(t)
        if len(res.rejected) > max_rejected:
            out.print(f"  [dim]… and {len(res.rejected) - max_rejected} more[/]")

    for w in res.warnings:
        out.print(f"[bold yellow]! {w}[/]")
    if rec.status == "rejected":
        err.print(f"[bold red]✗ run stopped, nothing loaded[/]: {rec.message}")
    elif rec.status == "guarded":
        err.print(
            f"[bold red]✗ run stopped by the mass-change guard, nothing written[/]: {rec.message}\n"
            "  If this is expected, run again with --allow-mass-change."
        )
    elif rec.status == "partial":
        err.print(f"[bold red]✗ {rec.failed} write(s) not applied[/] (see the report for each one)")
    elif rec.status == "dry_run":
        out.print("[yellow]Dry run: nothing written.[/]")
    out.print(f"[dim]run {rec.id}[/]")


@app.command()
def run(
    flow: Annotated[Path, typer.Argument(help="Flow file (.yaml)")],
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Use this file instead of the flow's path.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would change; write nothing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Run even if this file was already loaded.")
    ] = False,
    allow_mass_change: Annotated[
        bool,
        typer.Option("--allow-mass-change", help="Apply even if the guard limits are exceeded."),
    ] = False,
    report_dir: Annotated[
        Path | None,
        typer.Option("--report-dir", help="Write the HTML report here (default: no report)."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print a JSON summary instead (for schedulers).")
    ] = False,
    archive_dir: ArchiveOpt = None,
    no_archive: Annotated[
        bool, typer.Option("--no-archive", help="Don't keep a copy of the input file.")
    ] = False,
    state: StateOpt = None,
) -> None:
    """Run a flow: read, map, check, diff, then load (unless --dry-run).

    Exit codes: 0 done, 1 flow/source problem, 2 too many invalid rows,
    3 target problem, 4 stopped by the mass-change guard.
    """
    store = State(state)
    try:
        res = run_flow(
            flow, file=file, dry_run=dry_run, force=force,
            allow_mass_change=allow_mass_change, state=store,
            archive=None if no_archive else Archive(archive_dir),
        )  # fmt: skip
    except Exception as e:
        _fail(e)
    _finish(res, store, report_dir, as_json)


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument(help="Run id, or a unique start of it.")],
    flow: Annotated[
        Path | None, typer.Option("--flow", help="Flow file to use (default: the run's own).")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would change; write nothing.")
    ] = False,
    allow_mass_change: Annotated[
        bool,
        typer.Option("--allow-mass-change", help="Apply even if the guard limits are exceeded."),
    ] = False,
    report_dir: Annotated[
        Path | None, typer.Option("--report-dir", help="Write the HTML report here.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print a JSON summary instead.")] = False,
    state: StateOpt = None,
) -> None:
    """Run a past run's archived input file again, with the flow's current configuration.

    Same exit codes as `run`.
    """
    store = State(state)
    try:
        res = replay_run(
            run_id, state=store, flow_path=flow, dry_run=dry_run,
            allow_mass_change=allow_mass_change,
        )  # fmt: skip
    except Exception as e:
        _fail(e)
    _finish(res, store, report_dir, as_json)


_STATUS_EXIT = {"rejected": EXIT_CONTRACT, "guarded": EXIT_GUARD, "partial": EXIT_PARTIAL}


@app.command("run-all")
def run_all(
    flows: Annotated[list[Path], typer.Argument(help="Flow files (.yaml), in any order.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would change; write nothing.")
    ] = False,
    allow_mass_change: Annotated[
        bool,
        typer.Option("--allow-mass-change", help="Apply even if the guard limits are exceeded."),
    ] = False,
    report_dir: Annotated[
        Path | None, typer.Option("--report-dir", help="Write an HTML report per run here.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print a JSON list of run summaries instead.")
    ] = False,
    archive_dir: ArchiveOpt = None,
    no_archive: Annotated[
        bool, typer.Option("--no-archive", help="Don't keep copies of the input files.")
    ] = False,
    state: StateOpt = None,
) -> None:
    """Run several flows, each after the flows it looks up.

    A flow that looks up a flow which didn't succeed is not run (its lookups would read
    incomplete data). The exit code is the highest of the runs' exit codes.
    """
    try:
        order = run_order(flows)
    except Exception as e:
        _fail(e)
    if not as_json:
        out.print("[dim]order[/]  " + " → ".join(p.name for p in order))
        if dry_run:
            out.print(
                "[yellow]Dry run: lookups read the targets as they are now, not after the "
                "previews of the flows before them.[/]"
            )
    store = State(state)
    failed: dict[Path, str] = {}
    summaries: list[dict[str, Any]] = []
    worst = 0
    for path in order:
        if not as_json:
            out.rule(f"[bold]{path.name}[/]", align="left")
        blocked = _blocked_by(path, failed)
        if blocked:
            reason = f"not run: it looks up {blocked.name}, which {failed[blocked]}"
            failed[path] = "was not run"
            summaries.append({"flow_file": str(path), "status": "not_run", "message": reason})
            if not as_json:
                err.print(f"[bold red]✗ {reason}[/]")
            continue
        try:
            res = run_flow(
                path, dry_run=dry_run, allow_mass_change=allow_mass_change, state=store,
                archive=None if no_archive else Archive(archive_dir),
            )  # fmt: skip
        except Exception as e:
            code = EXIT_CONFIG
            try:
                _fail(e)
            except typer.Exit as stop:
                code = stop.exit_code
            failed[path] = "failed"
            summaries.append({"flow_file": str(path), "status": "error", "exit_code": code})
            worst = max(worst, code)
            continue
        report = write_report(store, res.record.id, report_dir) if report_dir else None
        code = _STATUS_EXIT.get(res.status, 0)
        if code:
            failed[path] = f"ended {res.status}"
        worst = max(worst, code)
        summaries.append({**res.record.__dict__, "warnings": res.warnings, "report": report})
        if not as_json:
            _summary(res)
            if report:
                out.print(f"[dim]report[/] {report}")
    if as_json:
        print(json.dumps(summaries, default=str, ensure_ascii=False))
    if worst:
        raise typer.Exit(worst)


def _blocked_by(path: Path, failed: dict[Path, str]) -> Path | None:
    try:
        spec = load_flow(path)
    except SpecError:
        return None  # the run reports it
    return next((d for d in dependencies(spec, path).values() if d in failed), None)


def _fail(e: Exception) -> NoReturn:
    """Print a known error and exit with its code; re-raise anything unexpected."""
    if isinstance(e, SpecError):
        _print_spec_error(e)
        raise typer.Exit(EXIT_CONFIG) from None
    if isinstance(
        e, SourceError | MappingError | ArchiveError | ReplayError | HookError | LinkError
    ):
        err.print(f"[bold red]✗[/] {e}")
        raise typer.Exit(EXIT_CONFIG) from None
    if isinstance(e, TargetError):
        err.print(f"[bold red]✗ target:[/] {e}")
        raise typer.Exit(EXIT_TARGET) from None
    raise e


def _finish(res: RunResult, store: State, report_dir: Path | None, as_json: bool) -> None:
    report = write_report(store, res.record.id, report_dir) if report_dir else None
    if as_json:
        summary = {**res.record.__dict__, "warnings": res.warnings}
        summary["report"] = str(report) if report else None
        print(json.dumps(summary, default=str, ensure_ascii=False))
    else:
        _summary(res)
        if report:
            out.print(f"[dim]report[/] {report}")
    if res.status == "rejected":
        raise typer.Exit(EXIT_CONTRACT)
    if res.status == "guarded":
        raise typer.Exit(EXIT_GUARD)
    if res.status == "partial":
        raise typer.Exit(EXIT_PARTIAL)


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="Run id, or a unique start of it.")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write here instead of stdout.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Full run detail as JSON.")] = False,
    state: StateOpt = None,
) -> None:
    """Rebuild the report of a past run from the run history."""
    try:
        text = render_json(State(state), run_id) if as_json else render_html(State(state), run_id)
    except ReportError as e:
        err.print(f"[bold red]✗[/] {e}")
        raise typer.Exit(EXIT_CONFIG) from None
    if output is None:
        print(text)
    else:
        output.write_text(text, encoding="utf-8")
        err.print(f"[bold green]✓[/] wrote {output}")


@app.command()
def runs(
    flow: Annotated[str | None, typer.Option("--flow", help="Only this flow.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    state: StateOpt = None,
) -> None:
    """Show recent runs."""
    t = Table(header_style="dim")
    for col in ("run", "flow", "status", "file", "+", "~", "-", "=", "rejected"):
        t.add_column(col, justify="right" if col in "+~-=rejected" else "left")
    colors = {
        "ok": "green", "dry_run": "yellow", "skipped": "dim", "rejected": "red", "guarded": "red",
        "partial": "red",
    }  # fmt: skip
    for r in State(state).history(flow, limit):
        t.add_row(
            r.id, r.flow, f"[{colors.get(r.status, 'white')}]{r.status}[/]",
            Path(r.file).name if r.file else "",
            str(r.created), str(r.updated), str(r.deactivated), str(r.unchanged), str(r.rejected),
        )  # fmt: skip
    out.print(t)


@app.command("map")
def map_columns(
    file: Annotated[Path, typer.Argument(help="A sample of the customer's file (CSV or Excel).")],
    entity: Annotated[
        str, typer.Option("--entity", "-e", help=f"Built-in entity: {', '.join(ENTITIES)}.")
    ] = "user",
    fields: Annotated[
        str | None,
        typer.Option("--fields", help="Custom target fields instead, comma-separated; key first."),
    ] = None,
    flow_name: Annotated[str | None, typer.Option("--flow", help="Name of the flow.")] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the draft flow file here.")
    ] = None,
) -> None:
    """Suggest a mapping for a file and write a draft flow file (offline, no AI)."""
    if fields:
        target: Entity = custom_entity(entity, [f.strip() for f in fields.split(",") if f.strip()])
    elif entity in ENTITIES:
        target = ENTITIES[entity]
    else:
        err.print(f"[bold red]✗[/] unknown entity {entity!r}; use one of {', '.join(ENTITIES)} "
                  "or --fields")  # fmt: skip
        raise typer.Exit(EXIT_CONFIG)
    try:
        read = read_file(file, FileSource(path=file.name))
    except SourceError as e:
        err.print(f"[bold red]✗[/] {e}")
        raise typer.Exit(EXIT_CONFIG) from None

    sample = read.df.drop("__row").head(SAMPLE_ROWS)
    suggestions = OfflineMatcher().suggest(sample, target)

    t = Table(header_style="dim", box=None, pad_edge=False)
    for col in ("field", "column", "confidence", "why"):
        t.add_column(col)
    for s in suggestions:
        if s.column is None:
            t.add_row(s.field, "[red]—[/]", "", "[red]no matching column[/]")
            continue
        color = "green" if s.confidence >= REVIEW else "yellow"
        why = s.reason + (f" · [yellow]{s.note}[/]" if s.note else "")
        if s.date_format:
            why += f" · format {s.date_format}"
        t.add_row(s.field, s.column, f"[{color}]{s.confidence:.2f}[/]", why)
    err.print(t)

    rel = Path(file.name)
    if output is not None:
        try:
            rel = file.resolve().relative_to(output.resolve().parent)
        except ValueError:
            rel = file.resolve()
    text = draft_flow(
        flow=flow_name or f"{target.name}s_from_{_slug(file.stem)}",
        entity=target,
        suggestions=suggestions,
        sample=sample,
        source_path=str(rel),
    )
    if output is None:
        print(text, end="")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        flagged = sum(1 for s in suggestions if s.needs_review)
        err.print(f"[bold green]✓[/] wrote {output}" + (
            f"  [yellow]({flagged} line(s) to review)[/]" if flagged else ""
        ))  # fmt: skip


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug if slug and slug[0].isalpha() else f"f_{slug}"
