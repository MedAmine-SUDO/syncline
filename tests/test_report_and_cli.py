import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from syncline.cli import app
from syncline.engine import run
from syncline.report.html import ReportError, render_html, render_json
from syncline.state import State

runner = CliRunner()


def cli(*args: str) -> tuple[int, str]:
    r = runner.invoke(app, list(args))
    return r.exit_code, " ".join(r.output.split())


def test_report_escapes_values_from_the_file(acme: Path, tmp_path: Path) -> None:
    f = acme / "evil.csv"
    f.write_text(
        "Matricule;Nom;Prénom;E-mail;Service;Date d'entrée\n"
        "A1;<script>alert(1)</script>;X;not-an-email;RH;01/01/2020\n"
        + "".join(f"A{i};N;P;p{i}@x.io;RH;01/01/2020\n" for i in range(2, 40)),
        encoding="utf-8",
    )
    state = State(tmp_path / "state.db")
    res = run(acme / "acme_users.yaml", file=f, state=state)
    html = render_html(state, res.record.id)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "not a valid e-mail address" in html


def test_report_is_rebuilt_from_history_only(acme: Path, day1: Path, tmp_path: Path) -> None:
    state = State(tmp_path / "state.db")
    run(acme / "acme_users.yaml", file=day1, state=state)
    res = run(acme / "acme_users.yaml", state=state)
    (acme / "employes_2026-11-02.csv").unlink()  # the source file is gone: report still works
    html = render_html(state, res.record.id[:-2])  # unique prefix is enough
    assert "Updated (2)" in html and "Created (6)" in html and "Deactivated" in html
    assert "Rows we couldn't load (13)" in html
    data = json.loads(render_json(state, res.record.id))
    assert data["run"]["status"] == "ok" and len(data["changes"]) == 12


def test_report_unknown_run(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="no single run"):
        render_html(State(tmp_path / "s.db"), "nope")


def test_cli_run_report_json_and_exit_codes(
    acme: Path, day1: Path, truncated: Path, tmp_path: Path
) -> None:
    flow, st, reports = str(acme / "acme_users.yaml"), str(tmp_path / "s.db"), tmp_path / "reports"

    code, out = cli("run", flow, "-f", str(day1), "--dry-run", "--state", st)
    assert code == 0 and "Would create 311" in out and "Dry run: nothing written" in out

    code, out = cli("run", flow, "-f", str(day1), "--state", st, "--report-dir", str(reports))
    assert code == 0 and "Done: create 311" in out and len(list(reports.glob("*.html"))) == 1

    code, out = cli("run", flow, "-f", str(day1), "--state", st)
    assert code == 0 and "skipped" in out

    code, out = cli("run", flow, "-f", str(truncated), "--state", st)
    assert code == 4 and "mass-change guard" in out and "Would create" in out

    r = runner.invoke(app, ["run", flow, "-f", str(truncated), "--state", st, "--json"])
    summary = json.loads(r.stdout)
    assert r.exit_code == 4 and summary["status"] == "guarded" and summary["deactivated"] > 100

    code, out = cli("runs", "--state", st, "--flow", "acme_users")
    assert code == 0 and "guarded" in out and "skipped" in out

    run_id = State(Path(st)).history(limit=1)[0].id
    out_file = tmp_path / "r.html"
    code, _ = cli("report", run_id, "--state", st, "-o", str(out_file))
    assert code == 0 and "stopped: changes too large" in out_file.read_text()
    code, out = cli("report", "nope", "--state", st)
    assert code == 1


def test_cli_run_contract_failure_exit_2(acme: Path, tmp_path: Path) -> None:
    flow = acme / "acme_users.yaml"
    flow.write_text(
        flow.read_text().replace("max_rejected_ratio: 0.05", "max_rejected_ratio: 0.01")
    )
    code, out = cli("run", str(flow), "--state", str(tmp_path / "s.db"))
    assert code == 2 and "nothing loaded" in out


def test_cli_run_config_errors(acme: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = str(tmp_path / "s.db")
    assert cli("run", str(tmp_path / "missing.yaml"), "--state", st)[0] == 1
    assert (
        cli("run", str(acme / "acme_users.yaml"), "-f", str(tmp_path / "no.csv"), "--state", st)[0]
        == 1
    )
    monkeypatch.delenv("APP_DB_URL")
    code, out = cli("run", str(acme / "acme_users.yaml"), "--state", st)
    assert code == 3 and "APP_DB_URL is not set" in out


def test_cli_mapping_error(acme: Path, tmp_path: Path) -> None:
    bad = acme / "other.csv"
    bad.write_text("a;b\n1;2\n", encoding="utf-8")
    code, out = cli(
        "run", str(acme / "acme_users.yaml"), "-f", str(bad), "--state", str(tmp_path / "s.db")
    )
    assert code == 1 and "not in the file" in out


def test_cli_version_and_schema() -> None:
    assert "syncline 0." in cli("--version")[1]
    r = runner.invoke(app, ["schema"])
    assert r.exit_code == 0 and json.loads(r.stdout)["title"] == "FlowSpec"
