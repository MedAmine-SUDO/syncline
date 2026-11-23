"""The run report: one self-contained HTML file per run, rebuilt from the run history.

Built only from what the state database stores (the run, its rejected rows and its
change log), so any past run can be reported again, identically, at any time.
Everything from the customer's file is HTML-escaped.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

from syncline import __version__
from syncline.state import RunRecord, State

MAX_ROWS_PER_SECTION = 500

STATUS_LABELS = {
    "ok": "applied",
    "dry_run": "dry run: nothing written",
    "skipped": "skipped",
    "rejected": "stopped: too many invalid rows",
    "guarded": "stopped: changes too large",
    "partial": "partly applied: some writes were refused",
}

_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))
_TEMPLATE = _env.from_string(
    files("syncline.report").joinpath("template.html.j2").read_text(encoding="utf-8")
)


class ReportError(Exception):
    pass


def _source_columns(rejected: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for r in rejected:
        for c in r["values"]:
            if c not in columns:
                columns.append(c)
    return columns


def _sections(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spec = [
        ("failed", "Writes the target refused", "what happened"),
        ("update", "Updated", "what changed"),
        ("create", "Created", "values"),
        ("deactivate", "Deactivated (no longer in the file)", "set"),
    ]
    out = []
    for action, title, label in spec:
        rows = [c for c in changes if c["action"] == action]
        if action == "failed" and not rows:
            continue  # only API targets can refuse single writes
        out.append({
            "title": title, "detail_label": label, "total": len(rows),
            "rows": rows[:MAX_ROWS_PER_SECTION],
        })  # fmt: skip
    return out


def load_run(
    state: State, run_id: str
) -> tuple[RunRecord, list[dict[str, Any]], list[dict[str, Any]]]:
    record = state.find(run_id)
    if record is None:
        raise ReportError(f"no single run matches {run_id!r} (see `syncline runs`)")
    got = state.get(record.id)
    assert got is not None
    return record, got[1], state.changes(record.id)


def render_html(state: State, run_id: str) -> str:
    record, rejected, changes = load_run(state, run_id)
    key = list(changes[0]["key"]) if changes else []
    return _TEMPLATE.render(
        run=record,
        status_label=STATUS_LABELS.get(record.status, record.status),
        verb="would be " if record.status in ("dry_run", "guarded") else "",
        file_name=Path(record.file).name if record.file else None,
        rejected=rejected,
        source_columns=_source_columns(rejected),
        sections=_sections(changes),
        key=key,
        version=__version__,
    )


def render_json(state: State, run_id: str) -> str:
    record, rejected, changes = load_run(state, run_id)
    payload = {"run": record.__dict__, "rejected": rejected, "changes": changes}
    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)


def write_report(state: State, run_id: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.html"
    path.write_text(render_html(state, run_id), encoding="utf-8")
    return path
