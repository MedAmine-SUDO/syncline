import shutil
from pathlib import Path

import polars as pl
import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from syncline.cli import app
from syncline.engine import run
from syncline.hooks import HookContext, HookError, call_hook, load_hook
from syncline.io.read import ROW
from syncline.spec import SpecError, load_flow
from syncline.state import State

ROOT = Path(__file__).parent.parent


def ctx(stage: str = "after_read") -> HookContext:
    return HookContext("f", "user", Path("x.csv"), "run1", False, stage)  # type: ignore[arg-type]


def table() -> pl.DataFrame:
    return pl.DataFrame({"a": ["1", "2", "3"]}).with_row_index(ROW, offset=2)


def hook_file(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "h.py"
    p.write_text("import polars as pl\n\n" + body, encoding="utf-8")
    return p


def test_load_hook_errors(tmp_path: Path) -> None:
    with pytest.raises(HookError, match="hook file not found"):
        load_hook("nope.py:f", tmp_path)
    hook_file(tmp_path, "def f(df, ctx):\n    return df\n")
    assert callable(load_hook("h.py:f", tmp_path))
    with pytest.raises(HookError, match="has no function 'g'"):
        load_hook("h.py:g", tmp_path)
    (tmp_path / "bad.py").write_text("def f(:\n")
    with pytest.raises(HookError, match=r"error while loading bad\.py: SyntaxError"):
        load_hook("bad.py:f", tmp_path)


@pytest.mark.parametrize(
    ("fn", "match"),
    [
        (lambda df, c: 1 / 0, r"hook ref \(after_read\) failed: ZeroDivisionError"),
        (lambda df, c: df.to_dicts(), "must return a polars DataFrame, got list"),
        (lambda df, c: df.drop(ROW), "dropped the __row column"),
        (lambda df, c: df.filter(pl.col("a") != "2"), r"removed line\(s\) 3"),
        (lambda df, c: pl.concat([df, df.head(1)]), "added or duplicated rows"),
    ],
)
def test_call_hook_rules(fn: object, match: str) -> None:
    with pytest.raises(HookError, match=match):
        call_hook(fn, "ref", table(), ctx())  # type: ignore[arg-type]


def test_after_read_may_add_columns_and_they_become_text() -> None:
    out = call_hook(
        lambda df, c: df.with_columns(pl.lit(7).alias("n")).reverse(), "ref", table(), ctx()
    )
    assert out.schema["n"] == pl.String
    assert out[ROW].to_list() == [2, 3, 4]  # original order restored


def test_after_map_must_keep_columns_and_types() -> None:
    typed = pl.DataFrame({"n": [1, 2]}).with_row_index(ROW)
    ok = call_hook(lambda df, c: df.with_columns(pl.col("n") * 10), "ref", typed, ctx("after_map"))
    assert ok["n"].to_list() == [10, 20]
    with pytest.raises(HookError, match="must keep the same columns and types"):
        call_hook(
            lambda df, c: df.with_columns(pl.col("n").cast(pl.String)),
            "ref",
            typed,
            ctx("after_map"),
        )


def test_bad_hook_reference_is_a_spec_error(tmp_path: Path) -> None:
    flow = (ROOT / "examples/acme/acme_users_with_hooks.yaml").read_text(encoding="utf-8")
    p = tmp_path / "f.yaml"
    p.write_text(flow.replace("hooks/acme.py:job_titles", "hooks/acme.py"), encoding="utf-8")
    with pytest.raises(SpecError, match=r"hooks\.after_read"):
        load_flow(p)


def test_example_hook_end_to_end(acme: Path, day1: Path, tmp_path: Path) -> None:
    shutil.copy(ROOT / "examples/acme/acme_users_with_hooks.yaml", acme)
    shutil.copytree(ROOT / "examples/acme/hooks", acme / "hooks")
    res = run(acme / "acme_users_with_hooks.yaml", file=day1, state=State(tmp_path / "s.db"))
    assert res.status == "ok" and res.record.created == 311
    url = f"sqlite:///{tmp_path / 'app.db'}"
    with sa.create_engine(url).connect() as c:
        titles = {
            r[0] for r in c.execute(sa.text("select distinct job_title from users_with_titles"))
        }
    assert "Ingénieur" in titles and "Chargé de clientèle" in titles
    assert not any("(" in t for t in titles)


def test_failing_hook_writes_nothing(acme: Path, day1: Path, tmp_path: Path) -> None:
    flow = acme / "acme_users_with_hooks.yaml"
    shutil.copy(ROOT / "examples/acme/acme_users_with_hooks.yaml", flow)
    (acme / "hooks").mkdir()
    (acme / "hooks" / "acme.py").write_text(
        "def job_titles(df, ctx):\n    raise ValueError('Poste column changed')\n"
    )
    r = CliRunner().invoke(
        app, ["run", str(flow), "-f", str(day1), "--state", str(tmp_path / "s.db")]
    )
    assert r.exit_code == 1 and "Poste column changed" in " ".join(r.output.split())
    assert not sa.inspect(sa.create_engine(f"sqlite:///{tmp_path / 'app.db'}")).has_table(
        "users_with_titles"
    )


def test_validate_checks_hooks(acme: Path) -> None:
    flow = acme / "acme_users_with_hooks.yaml"
    shutil.copy(ROOT / "examples/acme/acme_users_with_hooks.yaml", flow)
    r = CliRunner().invoke(app, ["validate", str(flow)])
    assert r.exit_code == 1 and "hook file not found" in r.output
    shutil.copytree(ROOT / "examples/acme/hooks", acme / "hooks")
    assert CliRunner().invoke(app, ["validate", str(flow)]).exit_code == 0
