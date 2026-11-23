from datetime import date

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncline.contract import ContractFailure, check
from syncline.io.read import ROW
from syncline.mapping.apply import MappingError, apply_mapping
from syncline.spec import Contract, FieldMapping


def frame(**cols: list[str | None]) -> pl.DataFrame:
    return pl.DataFrame(cols, schema={k: pl.String for k in cols}).with_row_index(ROW, offset=2)


def fm(**kw: object) -> FieldMapping:
    return FieldMapping.model_validate(kw)


def test_transforms_value_map_and_default() -> None:
    df = frame(Service=[" ventes ", "RH", None, "Ingénierie"])
    m = {
        "department": fm(
            **{
                "from": "Service",
                "transform": ["trim", "title"],
                "values": {"Ventes": "Sales", "Rh": "HR"},
                "default": "Unassigned",
            }
        )
    }
    out = apply_mapping(df, m).df["department"].to_list()
    assert out == ["Sales", "HR", "Unassigned", "Ingénierie"]


def test_title_case_names() -> None:
    df = frame(Nom=["LEFÈVRE", "jean-pierre", "mc donald"])
    out = apply_mapping(df, {"n": fm(**{"from": "Nom", "transform": ["title"]})}).df["n"]
    assert out.to_list()[0] == "Lefèvre"


def test_types_and_cell_errors() -> None:
    df = frame(
        d=["01/02/2024", "31/02/2021", None],
        e=["a@b.example", "not-an-email", None],
        i=["1 200", "x", "3"],
        f=["3,5", "abc", "2"],
        b=["Oui", "non", "maybe"],
    )
    m = {
        "d": fm(**{"from": "d", "type": "date", "format": "%d/%m/%Y"}),
        "e": fm(**{"from": "e", "type": "email"}),
        "i": fm(**{"from": "i", "type": "int"}),
        "f": fm(**{"from": "f", "type": "float"}),
        "b": fm(**{"from": "b", "type": "bool"}),
    }
    r = apply_mapping(df, m)
    assert r.df["d"].to_list() == [date(2024, 2, 1), None, None]
    assert r.df["i"].to_list() == [1200, None, 3]
    assert r.df["f"].to_list() == [3.5, None, 2.0]
    assert r.df["b"].to_list() == [True, False, None]
    errs = {(row, f): reason for row, f, _, reason in r.errors.iter_rows()}
    assert errs == {
        (3, "d"): "not a valid date (dd/mm/yyyy)",
        (3, "e"): "not a valid e-mail address",
        (3, "i"): "not a whole number",
        (3, "f"): "not a number",
        (4, "b"): "not yes/no",
    }


def test_missing_column_names_what_the_file_has() -> None:
    with pytest.raises(MappingError, match=r"not in the file: Email\. The file has: E-mail"):
        apply_mapping(frame(**{"E-mail": ["a"]}), {"email": fm(**{"from": "Email"})})


def test_contract_rejects_with_reasons_and_all_duplicates() -> None:
    df = frame(
        id=["A1", "A2", "A2", "A3", None], email=["a@x.io", "b@x.io", "c@x.io", None, "d@x.io"]
    )
    m = {"id": fm(**{"from": "id"}), "email": fm(**{"from": "email", "type": "email"})}
    c = Contract(required=["id", "email"], unique=["id"], max_rejected_ratio=1)
    r = check(apply_mapping(df, m), c)
    assert r.valid["id"].to_list() == ["A1"]
    reasons = dict(zip(r.rejected[ROW].to_list(), r.rejected["reasons"].to_list(), strict=True))
    assert reasons == {
        3: ["duplicate id 'A2' (rows 3, 4)"],
        4: ["duplicate id 'A2' (rows 3, 4)"],
        5: ["missing email"],
        6: ["missing id"],
    }


def test_allowed_values() -> None:
    df = frame(s=["active", "gone"])
    r = check(
        apply_mapping(df, {"s": fm(**{"from": "s"})}),
        Contract(allowed={"s": ["active", "inactive"]}, max_rejected_ratio=1),
    )
    assert r.rejected["reasons"].to_list() == [["s 'gone' is not one of: active, inactive"]]


def test_too_many_rejections_stop_the_run() -> None:
    df = frame(id=["A1", None, None])
    with pytest.raises(ContractFailure, match=r"2 of 3 rows rejected \(66.7%\)"):
        check(apply_mapping(df, {"id": fm(**{"from": "id"})}), Contract(required=["id"]))


@given(st.lists(st.text(alphabet=" aAéÉ\t-", max_size=8), min_size=1, max_size=20))
def test_trim_and_lower_are_idempotent(values: list[str]) -> None:
    m = {"x": fm(**{"from": "x", "transform": ["trim", "lower"]})}
    once = apply_mapping(frame(x=values), m).df["x"]
    twice = apply_mapping(frame(x=once.to_list()), m).df["x"]
    assert once.to_list() == twice.to_list()
