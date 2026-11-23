import os
from pathlib import Path

import polars as pl
import pytest

from syncline.io.read import ROW, SourceError, decode, detect_delimiter, find_file, read_file
from syncline.spec import FileSource

SRC = FileSource(path="*.csv")


def csv_file(tmp_path: Path, text: str, encoding: str = "utf-8", name: str = "f.csv") -> Path:
    p = tmp_path / name
    p.write_bytes(text.encode(encoding))
    return p


def test_cp1252_accents_and_semicolons(tmp_path: Path) -> None:
    p = csv_file(tmp_path, "Matricule;Prénom\nA1;Hélène\nA2;François\n", "cp1252")
    r = read_file(p, SRC)
    assert r.encoding == "cp1252"
    assert r.delimiter == ";"
    assert r.columns == ["Matricule", "Prénom"]
    assert r.df["Prénom"].to_list() == ["Hélène", "François"]


def test_utf8_bom_is_not_part_of_the_first_header(tmp_path: Path) -> None:
    p = tmp_path / "f.csv"
    p.write_bytes(b"\xef\xbb\xbf" + "id,name\n1,Zoé\n".encode())
    r = read_file(p, SRC)
    assert r.encoding == "utf-8-sig"
    assert r.columns == ["id", "name"]


def test_everything_is_text_and_leading_zeros_survive(tmp_path: Path) -> None:
    r = read_file(csv_file(tmp_path, "code,amount\n00123,1.50\n"), SRC)
    assert r.df.schema["code"] == pl.String
    assert r.df["code"][0] == "00123"


def test_blank_cells_are_null_and_row_numbers_match_the_file(tmp_path: Path) -> None:
    r = read_file(csv_file(tmp_path, "a,b\n1,  \n2,x\n"), SRC)
    assert r.df["b"].to_list() == [None, "x"]
    assert r.df[ROW].to_list() == [2, 3]


def test_headers_are_trimmed_and_duplicates_rejected(tmp_path: Path) -> None:
    assert read_file(csv_file(tmp_path, " a , b \n1,2\n"), SRC).columns == ["a", "b"]
    with pytest.raises(SourceError, match="duplicate column header"):
        read_file(csv_file(tmp_path, "a,a \n1,2\n", name="g.csv"), SRC)


def test_tab_and_pipe_delimiters() -> None:
    assert detect_delimiter("a\tb\tc\n1\t2\t3\n") == "\t"
    assert detect_delimiter("a|b|c\n1|2|3\n") == "|"


def test_explicit_encoding_wins_and_bad_one_is_explained() -> None:
    assert decode("é".encode("cp1252"), "cp1252") == ("é", "cp1252")
    with pytest.raises(SourceError, match="can't decode"):
        decode("é".encode("cp1252"), "utf-8")


def test_empty_file(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="empty"):
        read_file(csv_file(tmp_path, "  \n"), SRC)


def test_same_bytes_same_hash(tmp_path: Path) -> None:
    a = read_file(csv_file(tmp_path, "a\n1\n", name="a.csv"), SRC)
    b = read_file(csv_file(tmp_path, "a\n1\n", name="b.csv"), SRC)
    assert a.sha256 == b.sha256


def test_find_file_picks_last_name_not_newest_mtime(tmp_path: Path) -> None:
    old = csv_file(tmp_path, "a\n1\n", name="export_2026-11-02.csv")
    new = csv_file(tmp_path, "a\n1\n", name="export_2026-11-01.csv")
    os.utime(old, (0, 0))  # oldest mtime, but latest name
    assert find_file(FileSource(path="export_*.csv"), tmp_path) == old
    assert find_file(FileSource(path="export_*.csv"), tmp_path, override=new) == new


def test_find_file_no_match(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="no file matches"):
        find_file(FileSource(path="nothing_*.csv"), tmp_path)
