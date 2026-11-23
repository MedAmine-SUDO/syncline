"""Links between flows: lookups into another flow's target, and `syncline run-all`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
import yaml
from typer.testing import CliRunner

from syncline.cli import EXIT_CONFIG, EXIT_CONTRACT, app
from syncline.engine import run
from syncline.lookups import LinkError, run_order
from syncline.spec import FieldMapping, RestTarget, SpecError, load_flow
from syncline.state import State
from syncline.targets.rest import RestApi
from syncline.targets.sql import TargetError

ROOT = Path(__file__).parent.parent


def flat(output: str) -> str:
    return " ".join(output.split())


@pytest.fixture
def examples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "examples"
    for name in ("acme", "trainings", "participants"):
        shutil.copytree(ROOT / "examples" / name, d / name)
    monkeypatch.setenv("APP_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    return d


def table(tmp_path: Path, name: str) -> list[dict[str, Any]]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with engine.connect() as c:
        return [r._asdict() for r in c.execute(sa.text(f"select * from {name}"))]


def invoke(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def test_run_all_sorts_flows_and_resolves_lookups(examples: Path, tmp_path: Path) -> None:
    state = str(tmp_path / "s.db")
    invoke("run", str(examples / "acme/acme_users.yaml"), "-f",
           str(examples / "acme/employes_2026-11-01.csv"), "--state", state)  # fmt: skip
    invoke("run", str(examples / "trainings/trainings.yaml"), "-f",
           str(examples / "trainings/formations_2026-11-01.csv"), "--state", state)  # fmt: skip
    r = invoke(
        "run-all", str(examples / "participants/participants.yaml"),
        str(examples / "trainings/trainings.yaml"), str(examples / "acme/acme_users.yaml"),
        "--state", state,
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    out = flat(r.output)
    assert "order acme_users.yaml → trainings.yaml → participants.yaml" in out
    for reason in (
        "no acme_users record with email 'hugo.lefevre122@acme.example' among status 'active'",
        "no trainings record with code 'TRN-006' among status 'published'",
        "no acme_users record with email 'inconnu@acme.example'",
        "no trainings record with code 'TRN-099'",
    ):
        assert reason in out
    rows = {(p["user_id"], p["training_code"]): p for p in table(tmp_path, "participants")}
    assert len(rows) == 7
    assert ("ACM00321", "TRN-009") in rows  # a new hire created earlier in the same run-all
    assert ("ACM00010", "TRN-005") in rows  # trn-005: uppercased, published that same run
    assert rows[("ACM00002", "TRN-001")]["status"] == "completed"


def test_a_failed_dependency_blocks_the_flows_that_look_it_up(
    examples: Path, tmp_path: Path
) -> None:
    flow = examples / "trainings/trainings.yaml"
    data = yaml.safe_load(flow.read_text(encoding="utf-8"))
    data["contract"]["max_rejected_ratio"] = 0.05
    flow.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    r = invoke(
        "run-all", str(examples / "participants/participants.yaml"), str(flow),
        "--state", str(tmp_path / "s.db"), "--json",
    )  # fmt: skip
    assert r.exit_code == EXIT_CONTRACT, r.output
    summaries = json.loads(r.output)
    assert [s["status"] for s in summaries] == ["rejected", "not_run"]
    assert summaries[1]["message"] == "not run: it looks up trainings.yaml, which ended rejected"


def test_flows_that_look_each_other_up_are_a_cycle(tmp_path: Path) -> None:
    for me, other in (("a", "b"), ("b", "a")):
        (tmp_path / f"{me}.yaml").write_text(yaml.safe_dump(flow_data(me, lookup=f"{other}.yaml")))
    with pytest.raises(LinkError, match=r"cycle: a\.yaml → b\.yaml → a\.yaml"):
        run_order([tmp_path / "a.yaml", tmp_path / "b.yaml"])
    r = invoke("run-all", str(tmp_path / "a.yaml"), str(tmp_path / "b.yaml"))
    assert r.exit_code == EXIT_CONFIG and "cycle" in r.output


def test_run_all_dry_run_warns_and_a_broken_flow_is_reported(
    examples: Path, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("flow: Broken\n")
    r = invoke(
        "run-all", str(examples / "trainings/trainings.yaml"), str(broken), "--dry-run",
        "--state", str(tmp_path / "s.db"), "--no-archive",
    )  # fmt: skip
    assert r.exit_code == EXIT_CONFIG
    out = flat(r.output)
    assert "lookups read the targets as they are now" in out and "Dry run: nothing written" in out


# --- small flows to test lookups in isolation ----------------------------------------------


def flow_data(name: str, lookup: str | None = None, **field: Any) -> dict[str, Any]:
    mapping: dict[str, Any] = {"code": {"from": "code"}}
    if lookup:
        mapping["team"] = {"from": "team", "lookup": {"flow": lookup, "match": "name", **field}}
    else:
        mapping["name"] = {"from": "name"}
        mapping["size"] = {"from": "size", "type": "int"}
    return {
        "flow": name, "entity": name, "source": {"path": f"{name}.csv"}, "mapping": mapping,
        "contract": {"required": ["code"], "max_rejected_ratio": 1},
        "load": {"target": {"url": "env:APP_DB_URL", "table": name}, "key": ["code"],
                 "mode": "incremental"},
    }  # fmt: skip


@pytest.fixture
def teams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APP_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    (tmp_path / "teams.csv").write_text(
        "code,name,size\nT1,Red,3\nT2,Blue,x5\nT3,Blue,4\nT4,Green,\n"
    )
    (tmp_path / "teams.yaml").write_text(yaml.safe_dump(flow_data("teams")))
    data = flow_data("teams")
    data["mapping"]["size"]["type"] = "string"  # let "x5" in, to test typing after the lookup
    (tmp_path / "teams.yaml").write_text(yaml.safe_dump(data))
    run(tmp_path / "teams.yaml", state=State(tmp_path / "s.db"))
    return tmp_path


def members(dir: Path, csv: str, **lookup: Any) -> Any:
    (dir / "members.csv").write_text(csv)
    data = flow_data("members", lookup="teams.yaml", **lookup)
    if lookup.get("take") == "size":
        data["mapping"]["team"]["type"] = "int"
    (dir / "members.yaml").write_text(yaml.safe_dump(data))
    return run(dir / "members.yaml", state=State(dir / "s.db"))


def test_ambiguous_and_missing_matches(teams: Path) -> None:
    res = members(teams, "code,team\nM1,Red\nM2,Blue\nM3,Pink\nM4,\n", take="code")
    assert {r["row"]: r["reasons"] for r in res.rejected} == {
        3: ["2 teams records have name 'Blue': can't tell which one"],
        4: ["no teams record with name 'Pink'"],
    }
    assert res.checked is not None
    assert dict(res.checked.valid.select("code", "team").iter_rows()) == {"M1": "T1", "M4": None}


def test_the_value_taken_gets_the_fields_type(teams: Path) -> None:
    res = members(teams, "code,team\nM1,Red\nM2,Green\n", take="size")
    assert res.checked is not None
    assert res.checked.valid["team"].to_list() == [3, None]  # int, and Green has no size


def test_a_taken_value_that_doesnt_fit_the_type_is_a_reason(teams: Path) -> None:
    data = yaml.safe_load((teams / "teams.yaml").read_text())
    (teams / "teams.csv").write_text("code,name,size\nT9,Grey,x5\n")
    (teams / "teams.yaml").write_text(yaml.safe_dump(data))
    run(teams / "teams.yaml", state=State(teams / "s.db"))
    res = members(teams, "code,team\nM1,Grey\n", take="size")
    assert res.rejected[0]["reasons"] == ["size from teams: not a whole number"]


def test_where_filters_and_unknown_columns(teams: Path) -> None:
    res = members(teams, "code,team\nM1,Red\n", take="code", where={"size": 4})
    assert res.rejected[0]["reasons"] == ["no teams record with name 'Red' among size '4'"]
    with pytest.raises(TargetError, match=r"table teams has no column.s.: colour"):
        members(teams, "code,team\nM2,Red\n", take="colour")  # another file: not skipped


def test_a_flow_cant_look_up_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    (tmp_path / "self.csv").write_text("code,team\nA,x\n")
    data = flow_data("self", lookup="self.yaml")
    (tmp_path / "self.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(LinkError, match="can't look up its own records"):
        run(tmp_path / "self.yaml", state=State(tmp_path / "s.db"))


def test_lookup_into_a_target_that_doesnt_exist_yet_finds_nothing(teams: Path) -> None:
    (teams / "teams.yaml").write_text(yaml.safe_dump({**flow_data("teams"), "load": {
        "target": {"url": "env:APP_DB_URL", "table": "not_created"}, "key": ["code"],
        "mode": "incremental"}}))  # fmt: skip
    res = members(teams, "code,team\nM1,Red\n")
    assert res.rejected[0]["reasons"] == ["no teams record with name 'Red'"]


def test_validate_checks_the_looked_up_flow(teams: Path) -> None:
    members(teams, "code,team\nM1,Red\n")
    assert invoke("validate", str(teams / "members.yaml")).exit_code == 0
    (teams / "teams.yaml").unlink()
    r = invoke("validate", str(teams / "members.yaml"))
    assert r.exit_code == EXIT_CONFIG and "lookup flow" in flat(r.output)


def test_lookup_needs_from(tmp_path: Path) -> None:
    data = flow_data("members", lookup="teams.yaml")
    del data["mapping"]["team"]["from"]
    data["mapping"]["team"]["default"] = "x"
    (tmp_path / "f.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(SpecError, match="'lookup' needs 'from'"):
        load_flow(tmp_path / "f.yaml")


def test_rest_targets_can_be_looked_up_by_any_json_key() -> None:
    spec = RestTarget.model_validate({
        "type": "rest", "base_url": "http://api.test", "list": "/people",
        "create": {"method": "POST", "path": "/people"},
        "update": {"method": "PATCH", "path": "/people/{code}"},
        "fields": {"code": "externalId"},
    })  # fmt: skip
    items = [{"id": 41, "externalId": "A", "active": True}, {"id": 42, "externalId": "B"}]
    api = RestApi.open(
        spec, {"code": FieldMapping(source="c")}, ["code"], extra=None, run_id="r",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=items)),
    )  # fmt: skip
    df = api.columns(["code", "id", "active"])
    assert df.rows() == [("A", "41", "true"), ("B", "42", None)]
