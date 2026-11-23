"""REST target, against the fake API in examples/fake_api (real HTTP on a local port)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import yaml
from typer.testing import CliRunner

from syncline.cli import EXIT_PARTIAL, app
from syncline.engine import run
from syncline.spec import FieldMapping, RestTarget, load_flow
from syncline.state import State
from syncline.targets import rest
from syncline.targets.rest import RestApi, _from_json, _message, _retry_after
from syncline.targets.sql import TargetError

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("fake_api", ROOT / "examples/fake_api/server.py")
assert _spec and _spec.loader
fake_api = importlib.util.module_from_spec(_spec)
sys.modules["fake_api"] = fake_api  # dataclasses look the module up while it loads
_spec.loader.exec_module(fake_api)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    server = fake_api.make_server(fake_api.FakeApi())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ACME_API_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("ACME_API_TOKEN", "demo-token")
    yield server.api
    server.shutdown()
    server.server_close()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """No real waiting in tests; the waits are recorded instead."""
    waited: list[float] = []
    monkeypatch.setattr(rest, "_sleep", waited.append)
    return waited


@pytest.fixture
def flow(acme: Path) -> Path:
    shutil.copy(ROOT / "examples/acme/acme_users_api.yaml", acme)
    return acme / "acme_users_api.yaml"


def with_target(flow: Path, **changes: Any) -> Path:
    data = yaml.safe_load(flow.read_text(encoding="utf-8"))
    data["load"]["target"].update(changes)
    out = flow.with_name("variant.yaml")
    out.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return out


def writes(api: Any) -> list[tuple[str, str, Any]]:
    return [entry for entry in api.log if entry[0] != "GET"]


def test_acme_day1_day2_through_the_api_match_the_sql_demo(
    api: Any, flow: Path, acme: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    state = State(tmp_path / "s.db")
    day1 = run(flow, file=acme / "employes_2026-11-01.csv", state=state)
    assert (day1.status, day1.record.created, day1.record.rejected) == ("ok", 311, 12)
    person = api.records["ACM00001"]
    assert set(person) == {
        "externalId", "firstName", "lastName", "email", "department", "hireDate", "status"
    }  # fmt: skip
    assert person["hireDate"].count("-") == 2  # dates sent as ISO strings

    api.log.clear()
    day2 = run(flow, state=state)
    r = day2.record
    assert (r.status, r.created, r.updated, r.deactivated, r.unchanged) == ("ok", 6, 2, 4, 305)
    patches = [(p, b) for m, p, b in writes(api) if m == "PATCH"]
    assert ("/v1/people/ACM00122", {"status": "inactive"}) in patches
    assert any(b == {"department": "Engineering"} for _, b in patches)  # only what changed
    assert len(writes(api)) == 12

    api.log.clear()
    again = run(flow, state=state, force=True)
    assert again.diff is not None and again.diff.is_empty
    assert writes(api) == []  # nothing to change: nothing sent
    gets = [p for m, p, _ in api.log if m == "GET"]
    assert len(gets) == 4 and "page=4" in gets[-1]  # 317 records, pages of 100


def test_dry_run_reads_but_never_writes(api: Any, flow: Path, tmp_path: Path) -> None:
    res = run(flow, dry_run=True, state=State(tmp_path / "s.db"))
    assert res.status == "dry_run" and res.diff is not None
    assert res.diff.counts()["create"] == 313  # day 2's valid rows, into an empty API
    assert writes(api) == []


def test_temporary_errors_are_retried_and_retry_after_is_honoured(
    api: Any, flow: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    api.faults = [(503, {}), (429, {"Retry-After": "7"}), (502, {})]
    res = run(flow, state=State(tmp_path / "s.db"))
    assert res.status == "ok"
    assert 7.0 in sleeps  # Retry-After, not the backoff
    assert len(api.records) == 313


def test_a_lost_reply_to_a_post_is_retried_safely_with_an_idempotency_key(
    api: Any, flow: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    api.drop_after_write = 1  # the first create is applied, but its reply never arrives
    res = run(flow, state=State(tmp_path / "s.db"))
    assert res.status == "ok"
    assert len(api.records) == 313  # no duplicate, no 409
    keys = [b for m, _, b in writes(api)]
    assert keys[0] == keys[1]  # the same request was sent again


def test_without_idempotency_a_lost_post_reply_stops_instead_of_guessing(
    api: Any, flow: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    api.drop_after_write = 1
    res = run(with_target(flow, idempotency_header=None), state=State(tmp_path / "s.db"))
    assert res.status == "partial"
    assert res.record.created == 0 and res.record.failed == 313
    assert len(writes(api)) == 1  # not retried: it may have been applied
    message = res.record.message or ""
    assert message.startswith("stopped: POST /v1/people: RemoteProtocolError")
    assert "after" not in message.split(";")[0]  # not retried
    assert "312 not sent" in message


def test_a_refused_record_is_recorded_and_the_next_run_completes_it(
    api: Any, flow: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    state = State(tmp_path / "s.db")
    api.refuse = {"ACM00005"}
    first = run(flow, state=state)
    assert first.status == "partial"
    assert (first.record.created, first.record.failed) == (312, 1)
    assert "1 write(s) refused" in (first.record.message or "")
    log = state.changes(first.record.id)
    failed = [c for c in log if c["action"] == "failed"]
    assert failed == [{
        "action": "failed", "key": {"external_id": "ACM00005"},
        "detail": {
            "action": "create", "status": 422,
            "error": "HTTP 422: ACM00005 is locked in the application",
        },
    }]  # fmt: skip
    assert not any(c["action"] == "create" and c["key"] == {"external_id": "ACM00005"} for c in log)

    api.refuse = set()
    second = run(flow, state=state)  # same file: not skipped, the last run wasn't ok
    assert second.status == "ok" and second.record.created == 1


def test_systematic_refusals_stop_sending(
    api: Any, flow: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    api.write_status = 422
    res = run(with_target(flow, stop_after_failures=3), state=State(tmp_path / "s.db"))
    assert res.status == "partial" and res.record.failed == 313
    assert len(writes(api)) == 3
    assert "stopped after 3 refused writes in a row" in (res.record.message or "")
    assert "310 not sent" in (res.record.message or "")


def test_bad_credentials_on_write_stop_at_once(
    api: Any, flow: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    api.write_status = 403
    res = run(flow, state=State(tmp_path / "s.db"))
    assert res.status == "partial" and len(writes(api)) == 1
    assert "check the credentials" in (res.record.message or "")


def test_api_still_down_after_retries_stops(
    api: Any, flow: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    api.write_status = 503
    res = run(with_target(flow, max_retries=2), state=State(tmp_path / "s.db"))
    assert res.status == "partial" and len(writes(api)) == 3  # first try + 2 retries
    assert "HTTP 503 after 2 retries" in (res.record.message or "")


def test_put_sends_the_whole_record_and_keeps_fields_we_dont_manage(
    api: Any, flow: Path, acme: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    state = State(tmp_path / "s.db")
    put = with_target(flow, update={"method": "PUT", "path": "/v1/people/{external_id}"})
    run(put, file=acme / "employes_2026-11-01.csv", state=state)
    api.records["ACM00058"]["nickname"] = "Sam"  # set in the application, unknown to the flow
    run(put, state=state)
    assert api.records["ACM00058"]["nickname"] == "Sam"
    assert api.records["ACM00058"]["department"] == "Engineering"
    assert api.records["ACM00122"]["status"] == "inactive" and api.records["ACM00122"]["email"]


def test_cli_exit_code_and_report_for_a_partial_run(
    api: Any, flow: Path, tmp_path: Path, sleeps: list[float]
) -> None:
    api.refuse = {"ACM00005"}
    r = CliRunner().invoke(
        app,
        [
            "run",
            str(flow),
            "--state",
            str(tmp_path / "s.db"),
            "--report-dir",
            str(tmp_path / "rep"),
        ],
    )
    assert r.exit_code == EXIT_PARTIAL, r.output
    out = " ".join(r.output.split())
    assert "1 write(s) not applied" in out and "Sent:" in out
    html = next((tmp_path / "rep").glob("*.html")).read_text(encoding="utf-8")
    assert "Writes the target refused (1)" in html and "locked in the application" in html
    assert "partly applied" in html


# --- reading: pagination and what the API returns -------------------------------------------


def target(**kw: Any) -> RestTarget:
    base: dict[str, Any] = {
        "type": "rest", "base_url": "http://api.test", "list": "/items",
        "create": {"method": "POST", "path": "/items"},
        "update": {"method": "PATCH", "path": "/items/{id}"},
    }  # fmt: skip
    return RestTarget.model_validate({**base, **kw})


MAPPING = {"id": FieldMapping(source="ID"), "n": FieldMapping(source="N", type="int")}


def api_with(handler: Any, spec: RestTarget, **kw: Any) -> RestApi:
    transport = httpx.MockTransport(lambda req: handler(req))
    return RestApi.open(spec, MAPPING, ["id"], extra=None, run_id="r1", transport=transport, **kw)


def test_cursor_pagination() -> None:
    pages = {None: (["a", "b"], "c2"), "c2": (["c"], "c3"), "c3": ([], None)}

    def handler(req: httpx.Request) -> httpx.Response:
        ids, nxt = pages[req.url.params.get("cursor")]
        return httpx.Response(
            200, json={"results": [{"id": i, "n": "1"} for i in ids], "next": nxt}
        )

    spec = target(items="results", pagination={"type": "cursor", "next_cursor": "next"})
    df = api_with(handler, spec).read(["id"])
    assert df["id"].to_list() == ["a", "b", "c"] and df["n"].dtype == pl.Int64


def test_no_pagination_and_body_is_the_list() -> None:
    spec = target()
    df = api_with(lambda r: httpx.Response(200, json=[{"id": 1, "n": 2}]), spec).read(["id"])
    assert df.to_dicts() == [{"id": "1", "n": 2}]


@pytest.mark.parametrize(
    ("body", "spec_kw", "message"),
    [
        ({"x": 1}, {"items": "data"}, "expected a list of records at 'data'"),
        ([1], {}, "non-object item"),
        ([{"n": 1}], {}, "has no id"),
        ([{"id": "a"}, {"id": "a"}], {}, "two records with id = a"),
        ([{"id": "a", "n": "lots"}], {}, "returned 'lots' for n .int. on record a"),
        ([{"id": "a", "n": 1.5}], {}, "returned 1.5 for n"),
    ],
)
def test_what_the_api_returns_is_checked(body: Any, spec_kw: dict[str, Any], message: str) -> None:
    with pytest.raises(TargetError, match=message):
        api_with(lambda r: httpx.Response(200, json=body), target(**spec_kw)).read(["id"])


def test_pagination_that_doesnt_advance_is_an_error() -> None:
    same = [{"id": str(i)} for i in range(2)]
    spec = target(pagination={"type": "page", "size": 2})
    with pytest.raises(TargetError, match="pagination doesn't advance"):
        api_with(lambda r: httpx.Response(200, json=same), spec).read(["id"])

    def same_cursor(r: httpx.Request) -> httpx.Response:
        page = [{"id": r.url.params.get("cursor", "x")}]
        return httpx.Response(200, json={"items": page, "next_cursor": "same"})

    with pytest.raises(TargetError, match="the cursor repeats"):
        api_with(same_cursor, target(items="items", pagination={"type": "cursor"})).read(["id"])


def test_list_errors(sleeps: list[float]) -> None:
    with pytest.raises(TargetError, match="can't list /items: HTTP 404: not here"):
        api_with(lambda r: httpx.Response(404, json={"detail": "not here"}), target()).read(["id"])
    with pytest.raises(TargetError, match="didn't return JSON"):
        api_with(lambda r: httpx.Response(200, text="<html>"), target()).read(["id"])
    with pytest.raises(TargetError, match=r"HTTP 500 after 1 retries"):
        api_with(lambda r: httpx.Response(500, text="boom"), target(max_retries=1)).read(["id"])


def test_unreachable_api(sleeps: list[float]) -> None:
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    with pytest.raises(TargetError, match="can't connect"):
        api_with(refuse, target(max_retries=2)).read(["id"])
    assert len([s for s in sleeps if s >= 0.25]) == 2  # backoff between attempts (not rate limit)


def test_auth_styles_and_missing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200, json=[])

    monkeypatch.setenv("KEY", "k-123")
    api_with(handler, target(auth={"header": "X-Api-Key", "value": "env:KEY"})).read(["id"])
    assert seen["x-api-key"] == "k-123" and "authorization" not in seen
    with pytest.raises(TargetError, match="NOPE is not set"):
        api_with(handler, target(auth={"bearer": "env:NOPE"}))


def test_retry_after_parsing() -> None:
    assert _retry_after(None) is None
    assert _retry_after("3") == 3.0
    assert _retry_after("100000") == 120.0  # capped
    assert _retry_after("not a date") is None
    assert _retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past


# --- flow file validation ------------------------------------------------------------------


def test_flow_validation_of_rest_targets(flow: Path, tmp_path: Path) -> None:
    from syncline.spec import SpecError

    def problems(**target_changes: Any) -> str:
        data = yaml.safe_load(flow.read_text(encoding="utf-8"))
        data["load"]["target"].update(target_changes)
        p = tmp_path / "f.yaml"
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        with pytest.raises(SpecError) as e:
            load_flow(p)
        return " ".join(e.value.problems)

    assert "{email} is not a key field" in problems(
        update={"method": "PATCH", "path": "/p/{email}"}
    )
    assert "must name the record" in problems(update={"method": "PATCH", "path": "/p"})
    assert "load.target.auth.bearer: secrets don't go in flow files" in problems(
        auth={"bearer": "abc"}
    )
    assert "header and value go together" in problems(auth={"header": "X-Key"})
    assert "either bearer, or header" in problems(
        auth={"bearer": "env:A", "header": "X", "value": "env:B"}
    )
    assert "unknown type 'graphql'; use one of: sql, rest" in problems(type="graphql")
    assert json.dumps(load_flow(flow).load.target.model_dump(by_alias=True)).count('"list"') == 1


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        (None, "int", None),
        (True, "bool", True),
        ("FALSE", "bool", False),
        (3.0, "int", 3),
        ("2.5", "float", 2.5),
        ("2024-03-01T10:00:00Z", "date", date(2024, 3, 1)),
        (False, None, "False"),  # deactivation fields compare as text, like str(False)
        (12, "string", "12"),
    ],
)
def test_json_values_become_the_mapping_types(value: Any, kind: Any, expected: Any) -> None:
    assert _from_json(value, kind) == expected


@pytest.mark.parametrize(("value", "kind"), [("yes", "bool"), (True, "int"), (True, "float")])
def test_json_values_that_dont_fit(value: Any, kind: Any) -> None:
    with pytest.raises(ValueError):
        _from_json(value, kind)


def test_error_messages_from_api_responses() -> None:
    assert _message(httpx.Response(422, json={"errors": {"email": "bad"}})) == (
        'HTTP 422: {"email": "bad"}'
    )
    assert _message(httpx.Response(400, json={"other": 1})) == 'HTTP 400: {"other":1}'
    assert _message(httpx.Response(500, text="")) == "HTTP 500"
