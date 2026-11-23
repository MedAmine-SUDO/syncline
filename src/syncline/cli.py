"""Command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from syncline import __version__
from syncline.engine import RunResult, preview_changes
from syncline.engine import run as run_flow
from syncline.entities import ENTITIES, Entity, custom_entity
from syncline.io.read import SourceError, read_file
from syncline.mapping.apply import MappingError
from syncline.mapping.draft import draft_flow
from syncline.mapping.suggest import REVIEW, SAMPLE_ROWS, OfflineMatcher
from syncline.spec import FileSource, SpecError, json_schema, load_flow
from syncline.state import DEFAULT_STATE, State
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

StateOpt = Annotated[Path, typer.Option("--state", help="Run history database.")]


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
    except SpecError as e:
        _print_spec_error(e)
        raise typer.Exit(EXIT_CONFIG) from None
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
        verb = "Would" if rec.status == "dry_run" else "Done:"
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

    if rec.status == "rejected":
        err.print(f"[bold red]✗ run stopped, nothing loaded[/]: {rec.message}")
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
    state: StateOpt = DEFAULT_STATE,
) -> None:
    """Run a flow: read, map, check, diff, then load (unless --dry-run)."""
    try:
        res = run_flow(flow, file=file, dry_run=dry_run, force=force, state=State(state))
    except SpecError as e:
        _print_spec_error(e)
        raise typer.Exit(EXIT_CONFIG) from None
    except (SourceError, MappingError) as e:
        err.print(f"[bold red]✗[/] {e}")
        raise typer.Exit(EXIT_CONFIG) from None
    except TargetError as e:
        err.print(f"[bold red]✗ target:[/] {e}")
        raise typer.Exit(EXIT_TARGET) from None
    _summary(res)
    if res.status == "rejected":
        raise typer.Exit(EXIT_CONTRACT)


@app.command()
def runs(
    flow: Annotated[str | None, typer.Option("--flow", help="Only this flow.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    state: StateOpt = DEFAULT_STATE,
) -> None:
    """Show recent runs."""
    t = Table(header_style="dim")
    for col in ("run", "flow", "status", "file", "+", "~", "-", "=", "rejected"):
        t.add_column(col, justify="right" if col in "+~-=rejected" else "left")
    colors = {"ok": "green", "dry_run": "yellow", "skipped": "dim", "rejected": "red"}
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
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug if slug and slug[0].isalpha() else f"f_{slug}"
