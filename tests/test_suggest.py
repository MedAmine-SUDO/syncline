from pathlib import Path

import polars as pl
import pytest
import yaml
from typer.testing import CliRunner

from syncline.cli import app
from syncline.entities import USER, custom_entity
from syncline.mapping.draft import draft_flow, q
from syncline.mapping.suggest import (
    OfflineMatcher,
    detect_date_format,
    distinct_values,
    looks_like_email,
    name_score,
    normalize,
)
from syncline.spec import load_flow

ACME = Path(__file__).parent.parent / "examples" / "acme" / "employes_2026-11-01.csv"


def sample(**cols: list[str | None]) -> pl.DataFrame:
    return pl.DataFrame(cols, schema={k: pl.String for k in cols})


def by_field(df: pl.DataFrame, fields: list[str] | None = None) -> dict:
    entity = custom_entity("thing", fields) if fields else USER
    return {s.field: s for s in OfflineMatcher().suggest(df, entity)}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Date d'entrée", "date entree"),
        ("E-MAIL", "e mail"),
        ("firstName", "first name"),
        ("  N° de  Téléphone ", "n telephone"),
        ("de", "de"),  # only stopwords: keep them rather than return nothing
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_name_score_rules() -> None:
    assert name_score("PRÉNOM", ["prenom"])[0] == 1.0
    assert name_score("Name First", ["first name"])[0] == 0.95
    assert name_score("FirstName", ["first name"])[0] == 1.0  # camelCase split
    assert name_score("firstname", ["first name"])[0] == 0.95
    assert 0.6 < name_score("Matricul", ["matricule"])[0] < 0.95
    assert name_score("Zzz", ["matricule"])[0] < 0.3


def test_acme_headers_all_found() -> None:
    from syncline.io.read import read_file
    from syncline.spec import FileSource

    df = read_file(ACME, FileSource(path="x")).df.drop("__row")
    s = by_field(df)
    assert {f: x.column for f, x in s.items() if x.column} == {
        "external_id": "Matricule", "first_name": "Prénom", "last_name": "Nom",
        "email": "E-mail", "department": "Service", "job_title": "Poste",
        "hire_date": "Date d'entrée",
    }  # fmt: skip
    assert s["hire_date"].date_format == "%d/%m/%Y"
    assert s["phone"].column is None and s["phone"].needs_review


def test_email_found_by_values_when_header_is_meaningless() -> None:
    df = sample(Col7=["a@x.io", "b@x.io", "c@x.io"], ID=["1", "2", "3"])
    s = by_field(df)
    assert s["email"].column == "Col7"
    assert s["email"].reason == "values look like e-mail addresses"
    assert s["external_id"].column == "ID"


def test_a_column_is_used_once() -> None:
    s = by_field(sample(Manager=["x"]), ["manager", "manager_id"])
    assert [x.column for x in s.values()].count("Manager") == 1


def test_weak_match_is_not_used() -> None:
    assert by_field(sample(Quantity=["1"]))["external_id"].column is None


def test_header_says_email_but_values_disagree() -> None:
    s = by_field(sample(Email=["yes", "no", "maybe"]))["email"]
    assert s.confidence <= 0.7 and s.needs_review and "aren't e-mail" in (s.note or "")


def test_dates_ambiguous_and_not_dates() -> None:
    assert detect_date_format(["31/01/2024", "15/12/2023"]) == ("%d/%m/%Y", None)
    assert detect_date_format(["2024-01-31"]) == ("%Y-%m-%d", None)
    fmt, note = detect_date_format(["01/02/2024", "03/04/2024"])
    assert fmt == "%d/%m/%Y" and note and "ambiguous" in note
    assert detect_date_format(["hello"]) == (None, None)
    assert detect_date_format([]) == (None, None)
    s = by_field(sample(**{"Hire date": ["soon", "later"]}))["hire_date"]
    assert s.needs_review and s.note == "values don't look like dates"


def test_looks_like_email() -> None:
    assert looks_like_email(["a@b.co"] * 8 + ["x", "y"])
    assert not looks_like_email(["a@b.co", "x"])
    assert not looks_like_email([])


def test_distinct_values_groups_case() -> None:
    v = distinct_values(sample(d=["RH", "rh", "RH", "Ventes", " ventes "]), "d")
    assert v is not None
    assert v.values == [("RH", 3), ("Ventes", 2)]
    assert v.case_variants
    assert distinct_values(sample(d=[str(i) for i in range(30)]), "d") is None


def test_yaml_quoting() -> None:
    assert q("Date d'entrée") == "Date d'entrée"
    assert q("%d/%m/%Y") == "'%d/%m/%Y'"
    assert q("a: b") == "'a: b'"
    assert yaml.safe_load(f"x: {q('yes')}")["x"] == "yes"  # not turned into True


def test_draft_is_a_valid_flow(tmp_path: Path) -> None:
    from syncline.io.read import read_file
    from syncline.spec import FileSource

    df = read_file(ACME, FileSource(path="x")).df.drop("__row")
    text = draft_flow(
        flow="acme_users", entity=USER, suggestions=OfflineMatcher().suggest(df, USER),
        sample=df, source_path=ACME.name,
    )  # fmt: skip
    p = tmp_path / "flow.yaml"
    p.write_text(text, encoding="utf-8")
    spec = load_flow(p)
    assert spec.mapping["hire_date"].format == "%d/%m/%Y"
    assert spec.contract.unique == ["external_id", "email"]
    assert "REVIEW: no matching column found" in text  # phone, manager_id, end_date
    assert "add `lower` or `upper`" in text  # department values differ by case


def test_draft_without_key_says_so() -> None:
    df = sample(Foo=["1"])
    text = draft_flow(
        flow="x", entity=USER, suggestions=OfflineMatcher().suggest(df, USER),
        sample=df, source_path="f.csv",
    )  # fmt: skip
    assert "the key field 'external_id' has no column" in text


def test_cli_map_writes_relative_path(tmp_path: Path) -> None:
    data = tmp_path / "in"
    data.mkdir()
    f = data / "export.csv"
    f.write_text("Matricule;E-mail\nA1;a@x.io\n", encoding="utf-8")
    out = tmp_path / "flows" / "f.yaml"
    r = CliRunner().invoke(app, ["map", str(f), "-o", str(out)])
    assert r.exit_code == 0, r.output
    spec = load_flow(out)
    assert spec.flow == "users_from_export"
    assert spec.source.path == str(f.resolve())  # not below the flow's folder: absolute


def test_cli_map_custom_fields_and_stdout(tmp_path: Path) -> None:
    f = tmp_path / "products.csv"
    f.write_text("SKU,Label,Price\nA,Chair,10\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["map", str(f), "-e", "product", "--fields", "sku,label,price"])
    assert r.exit_code == 0, r.output
    assert "sku: { from: SKU" in r.output and "table: products" in r.output


def test_cli_map_errors(tmp_path: Path) -> None:
    assert CliRunner().invoke(app, ["map", str(ACME), "-e", "nope"]).exit_code == 1
    assert CliRunner().invoke(app, ["map", str(tmp_path / "missing.csv")]).exit_code == 1


def test_custom_entity_needs_fields() -> None:
    with pytest.raises(ValueError, match="at least one"):
        custom_entity("x", [])
