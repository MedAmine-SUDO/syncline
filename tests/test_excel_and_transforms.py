from pathlib import Path

import polars as pl
import pytest

from syncline.io.read import ROW, SourceError, read_file
from syncline.mapping.apply import apply_mapping
from syncline.spec import FieldMapping, FileSource


def test_excel_is_read_as_text_with_row_numbers(tmp_path: Path) -> None:
    p = tmp_path / "export.xlsx"
    pl.DataFrame(
        {"Matricule": ["00012", "00013"], " Prénom ": ["Zoé", None], "Âge": [31, 40]}
    ).write_excel(p)
    r = read_file(p, FileSource(path="*.xlsx"))
    assert r.encoding is None and r.delimiter is None
    assert r.columns == ["Matricule", "Prénom", "Âge"]  # headers trimmed
    assert r.df["Matricule"].to_list() == ["00012", "00013"]  # leading zeros kept
    assert r.df["Âge"].to_list() == ["31", "40"]  # numbers arrive as text, typed by the mapping
    assert r.df["Prénom"].to_list() == ["Zoé", None]
    assert r.df[ROW].to_list() == [2, 3]


def test_excel_named_sheet_and_errors(tmp_path: Path) -> None:
    p = tmp_path / "export.xlsx"
    pl.DataFrame({"a": ["1"]}).write_excel(p, worksheet="Salariés")
    assert read_file(p, FileSource(path="x", sheet="Salariés")).columns == ["a"]
    with pytest.raises(SourceError, match=r"can't read export\.xlsx"):
        read_file(p, FileSource(path="x", sheet="Nope"))
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not a zip")
    with pytest.raises(SourceError, match=r"can't read broken\.xlsx"):
        read_file(broken, FileSource(path="x"))


def test_unreadable_source(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="can't read"):
        read_file(tmp_path, FileSource(path="x"))  # a folder, not a file


def test_ragged_csv_is_explained(tmp_path: Path) -> None:
    p = tmp_path / "f.csv"
    p.write_text("a,b\n1,2,3,4\n", encoding="utf-8")
    with pytest.raises(SourceError, match=r"can't parse f\.csv"):
        read_file(p, FileSource(path="x"))


@pytest.mark.parametrize(
    ("transform", "value", "expected"),
    [
        (["upper"], "rh", "RH"),
        (["collapse_spaces"], "  Jean   Pierre ", "Jean Pierre"),
        (["digits_only"], "+216 94-842 635", "21694842635"),
        (["trim", "lower"], "  A@B.IO ", "a@b.io"),
        (["digits_only"], "n/a", None),  # nothing left: empty becomes null
    ],
)
def test_transforms(transform: list[str], value: str, expected: str | None) -> None:
    df = pl.DataFrame({"x": [value]}).with_row_index(ROW, offset=2)
    m = {"x": FieldMapping.model_validate({"from": "x", "transform": transform})}
    assert apply_mapping(df, m).df["x"].to_list() == [expected]
