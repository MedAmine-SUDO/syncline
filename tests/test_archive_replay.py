"""Raw-file archive, replay, and upgrading a run history written by an older version."""

import os
import stat
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from typer.testing import CliRunner

from syncline.archive import Archive, ArchiveError, sha256_of
from syncline.cli import app
from syncline.engine import ReplayError, replay, run
from syncline.state import State


def test_store_is_content_addressed_read_only_and_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "export.CSV"
    src.write_bytes(b"a;b\n1;2\n")
    sha = sha256_of(src)
    archive = Archive(tmp_path / "archive")
    kept = archive.store("flow", src, sha)
    assert kept == tmp_path / "archive" / "flow" / f"{sha}.csv"
    assert kept.read_bytes() == src.read_bytes()
    assert not os.stat(kept).st_mode & stat.S_IWUSR  # read-only
    copy = tmp_path / "copy.csv"
    copy.write_bytes(src.read_bytes())
    assert archive.store("flow", copy, sha) == kept  # same content: stored once
    assert len(list((tmp_path / "archive" / "flow").iterdir())) == 1


def test_store_refuses_a_file_that_changed_since_it_was_read(tmp_path: Path) -> None:
    src = tmp_path / "f.csv"
    src.write_bytes(b"new content")
    with pytest.raises(ArchiveError, match="changed while it was being read"):
        Archive(tmp_path / "a").store("flow", src, "0" * 64)
    assert not list((tmp_path / "a" / "flow").glob("*"))  # nothing half-written left


def test_verify_detects_tampering_and_missing_files(tmp_path: Path) -> None:
    src = tmp_path / "f.csv"
    src.write_bytes(b"x\n1\n")
    kept = Archive(tmp_path / "a").store("flow", src, sha256_of(src))
    os.chmod(kept, stat.S_IWUSR | stat.S_IRUSR)
    kept.write_bytes(b"x\n2\n")
    with pytest.raises(ArchiveError, match="doesn't match its hash"):
        Archive.verify(kept, sha256_of(src))
    with pytest.raises(ArchiveError, match="missing"):
        Archive.verify(tmp_path / "nope.csv", "0" * 64)


def test_run_keeps_the_input_and_records_where(acme: Path, day1: Path, tmp_path: Path) -> None:
    state, archive = State(tmp_path / "s.db"), Archive(tmp_path / "archive")
    res = run(acme / "acme_users.yaml", file=day1, state=state, archive=archive)
    assert res.record.archived and Path(res.record.archived).read_bytes() == day1.read_bytes()
    assert res.record.flow_path == str((acme / "acme_users.yaml").resolve())
    assert state.find(res.record.id).archived == res.record.archived  # type: ignore[union-attr]


def test_replay_runs_the_same_bytes_through_the_fixed_flow(
    acme: Path, day1: Path, tmp_path: Path
) -> None:
    flow = acme / "acme_users.yaml"
    state, archive = State(tmp_path / "s.db"), Archive(tmp_path / "archive")
    data = yaml.safe_load(flow.read_text(encoding="utf-8"))
    values = data["mapping"]["department"].pop("values")  # forget the value map
    flow.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    first = run(flow, file=day1, state=state, archive=archive)
    day1.unlink()  # the customer's file is gone

    data["mapping"]["department"]["values"] = values  # fix the flow
    flow.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    preview = replay(first.record.id, state=state, dry_run=True)
    fixed = replay(first.record.id[:-1], state=state)

    assert preview.record.updated == fixed.record.updated == 311  # every department translated
    assert fixed.record.message == f"replay of run {first.record.id}"
    assert fixed.record.sha256 == first.record.sha256
    assert replay(first.record.id, state=state).diff.is_empty  # type: ignore[union-attr]


def test_replay_errors(acme: Path, day1: Path, tmp_path: Path) -> None:
    state, archive = State(tmp_path / "s.db"), Archive(tmp_path / "archive")
    with pytest.raises(ReplayError, match="no single run"):
        replay("nope", state=state)

    plain = run(acme / "acme_users.yaml", file=day1, state=state)  # no archive
    with pytest.raises(ReplayError, match="no archived input file"):
        replay(plain.record.id, state=state)

    kept = run(acme / "acme_users.yaml", file=day1, state=state, archive=archive, force=True)
    other = acme / "other.yaml"
    other.write_text(
        (acme / "acme_users.yaml").read_text().replace("flow: acme_users", "flow: other")
    )
    with pytest.raises(ReplayError, match="was 'acme_users'"):
        replay(kept.record.id, state=state, flow_path=other)

    assert kept.record.archived
    os.chmod(kept.record.archived, stat.S_IWUSR | stat.S_IRUSR)
    Path(kept.record.archived).write_text("tampered")
    with pytest.raises(ArchiveError, match="doesn't match its hash"):
        replay(kept.record.id, state=state)


def test_old_history_is_upgraded_in_place(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sa.create_engine(f"sqlite:///{db}").begin() as c:  # the v0.1.0 runs table
        c.execute(sa.text(
            "create table runs (id varchar primary key, flow varchar not null, file varchar,"
            " sha256 varchar, started_at datetime not null, finished_at datetime,"
            " status varchar not null, total integer, created integer, updated integer,"
            " deactivated integer, unchanged integer, rejected integer, message text)"
        ))  # fmt: skip
        c.execute(sa.text(
            "insert into runs values ('r1', 'f', null, 'abc', '2026-11-23 10:00:00', null, 'ok',"
            " 1, 1, 0, 0, 0, 0, null)"
        ))  # fmt: skip
    state = State(db)
    old = state.find("r1")
    assert old is not None and old.archived is None and old.flow_path is None
    with pytest.raises(ReplayError, match="no archived input file"):
        replay("r1", state=state)


def test_cli_replay(acme: Path, day1: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    st, ar = str(tmp_path / "s.db"), str(tmp_path / "archive")
    r = runner.invoke(
        app, ["run", str(acme / "acme_users.yaml"), "-f", str(day1), "--state", st, "--archive", ar]
    )
    assert r.exit_code == 0, r.output
    run_id = State(Path(st)).history(limit=1)[0].id
    r = runner.invoke(app, ["replay", run_id, "--state", st, "--dry-run"])
    assert r.exit_code == 0 and "Would create 0" in " ".join(r.output.split())
    r = runner.invoke(app, ["replay", "nope", "--state", st])
    assert r.exit_code == 1 and "no single run" in r.output
    r = runner.invoke(
        app, ["run", str(acme / "acme_users.yaml"), "--state", st, "--no-archive", "--json"]
    )
    assert r.exit_code == 0 and '"archived": null' in r.stdout


def test_cli_run_archives_by_default(acme: Path, day1: Path, tmp_path: Path) -> None:
    r = CliRunner().invoke(app, ["run", str(acme / "acme_users.yaml"), "-f", str(day1), "--json"])
    assert r.exit_code == 0, r.output
    assert len(list((tmp_path / "default-archive" / "acme_users").glob("*.csv"))) == 1
