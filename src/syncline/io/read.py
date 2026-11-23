"""Reading customer files: find the file, detect how it's encoded, load it as text.

Every column is read as text. Types are applied later by the mapping, so a bad value
becomes a rejected row with a reason instead of a crash or a silently wrong number.
Each row keeps its line number in the original file (`__row`) for the report.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from syncline.spec import FileSource

ROW = "__row"  # line number in the source file (header = line 1)
_DELIMITERS = [",", ";", "\t", "|"]
_EXCEL = {".xlsx", ".xlsm", ".xls"}


class SourceError(Exception):
    """The source file can't be found or read."""


@dataclass(frozen=True)
class ReadResult:
    df: pl.DataFrame  # all source columns as strings, plus ROW
    path: Path
    sha256: str
    encoding: str | None  # None for Excel
    delimiter: str | None  # None for Excel

    @property
    def columns(self) -> list[str]:
        return [c for c in self.df.columns if c != ROW]


def find_file(source: FileSource, base_dir: Path, override: Path | None = None) -> Path:
    """The file to read: `override` if given, else the last match of the glob by name.

    Files are picked by name, not by modification time: dated names
    (employes_2026-11-02.csv) sort correctly, and mtimes change on copy or checkout.
    """
    if override is not None:
        if not override.is_file():
            raise SourceError(f"file not found: {override}")
        return override
    pattern = Path(source.path)
    root = pattern.parent if pattern.is_absolute() else base_dir / pattern.parent
    matches = sorted(p for p in root.glob(pattern.name) if p.is_file())
    if not matches:
        raise SourceError(f"no file matches {root / pattern.name}")
    return matches[-1]


def decode(data: bytes, encoding: str | None = None) -> tuple[str, str]:
    """Bytes to text. Tries UTF-8 (with or without BOM), then Windows-1252."""
    if encoding:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError) as e:
            raise SourceError(f"can't decode file as {encoding}: {e}") from None
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="strict"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"  # never fails; last resort


def detect_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        header = text.splitlines()[0] if text else ""
        return max(_DELIMITERS, key=header.count)


def _clean_headers(df: pl.DataFrame, path: Path) -> pl.DataFrame:
    names = [c.strip() for c in df.columns]
    if any(not n for n in names):
        raise SourceError(f"{path.name}: a column has no header")
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SourceError(f"{path.name}: duplicate column header(s): {', '.join(dupes)}")
    return df.rename(dict(zip(df.columns, names, strict=True)))


def read_file(path: Path, source: FileSource) -> ReadResult:
    try:
        data = path.read_bytes()
    except OSError as e:
        raise SourceError(f"can't read {path}: {e}") from None
    digest = hashlib.sha256(data).hexdigest()

    encoding: str | None
    delimiter: str | None
    if path.suffix.lower() in _EXCEL:
        try:
            df = pl.read_excel(
                path,
                sheet_name=source.sheet,
                engine="calamine",
                infer_schema_length=0,  # everything as text
            )
        except ModuleNotFoundError:
            raise SourceError(
                "reading Excel needs the extra: pip install 'syncline[excel]'"
            ) from None
        except Exception as e:
            raise SourceError(f"can't read {path.name}: {e}") from None
        df = df.with_columns(pl.all().cast(pl.String))
        encoding = delimiter = None
    else:
        text, encoding = decode(data, source.encoding)
        if not text.strip():
            raise SourceError(f"{path.name} is empty")
        delimiter = source.delimiter or detect_delimiter(text)
        try:
            df = pl.read_csv(
                text.encode("utf-8"),
                separator=delimiter,
                infer_schema=False,  # everything as text
                truncate_ragged_lines=False,
            )
        except pl.exceptions.PolarsError as e:
            raise SourceError(f"can't parse {path.name} as CSV ({delimiter!r}): {e}") from None

    df = _clean_headers(df, path)
    # Blank cells and whitespace-only cells mean "no value".
    df = df.with_columns(
        pl.when(pl.col(c).str.strip_chars() == "").then(None).otherwise(pl.col(c)).alias(c)
        for c in df.columns
    )
    df = df.with_row_index(ROW, offset=2)  # header is line 1
    return ReadResult(df=df, path=path, sha256=digest, encoding=encoding, delimiter=delimiter)
