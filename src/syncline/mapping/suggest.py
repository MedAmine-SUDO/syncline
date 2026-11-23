"""Suggest a mapping from a file's headers and a sample of its values, offline.

How a column is matched to a field, strongest signal first:
1. Names: the header, normalized, against the field name and its synonyms
   (exact, same words in another order, same letters without spaces, then fuzzy).
2. Values: a column whose values are mostly e-mail addresses is the e-mail field;
   date columns get their format detected from the values, and ambiguous ones
   (01/02/2024: January or February?) are flagged instead of guessed.

Every suggestion carries a confidence. Low-confidence matches are left out and
flagged for review: a wrong mapping silently loads wrong data, a missing one doesn't.

The `MappingProvider` protocol is the seam for other strategies (an LLM, a mapping
learned from past files): anything that returns `Suggestion`s plugs into `syncline map`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Protocol

import polars as pl

from syncline.entities import Entity, EntityField
from syncline.mapping.apply import EMAIL_RE

ACCEPT = 0.60  # below this, the column isn't used
REVIEW = 0.85  # below this, the suggestion is kept but flagged
SAMPLE_ROWS = 200

DATE_FORMATS = ["%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y"]
_STOPWORDS = {"de", "du", "d", "la", "le", "l", "des", "the", "of", "a"}


@dataclass(frozen=True)
class Suggestion:
    field: str
    column: str | None  # None: no column found
    confidence: float
    reason: str
    date_format: str | None = None
    note: str | None = None  # anything the reviewer should know

    @property
    def needs_review(self) -> bool:
        return self.column is None or self.confidence < REVIEW or self.note is not None


class MappingProvider(Protocol):
    def suggest(self, sample: pl.DataFrame, entity: Entity) -> list[Suggestion]: ...


def normalize(text: str) -> str:
    """'Date d'entrée' -> 'date entree'; 'E-MAIL' -> 'e mail'; 'firstName' -> 'first name'."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # camelCase
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    kept = [w for w in words if w not in _STOPWORDS]
    return " ".join(kept or words)


def name_score(header: str, candidates: list[str]) -> tuple[float, str]:
    """Best score of a header against a field's names, with the rule that produced it."""
    h = normalize(header)
    best: tuple[float, str] = (0.0, "")
    for cand in candidates:
        c = normalize(cand)
        if not h or not c:
            continue
        if h == c:
            score = (1.0, f"header is '{cand}'")
        elif sorted(h.split()) == sorted(c.split()):
            score = (0.95, f"same words as '{cand}'")
        elif h.replace(" ", "") == c.replace(" ", ""):
            score = (0.95, f"same as '{cand}' without spaces")
        else:
            ratio = SequenceMatcher(None, h, c).ratio()
            score = (round(ratio * 0.9, 2), f"close to '{cand}'")
        best = max(best, score, key=lambda s: s[0])
    return best


def _values(sample: pl.DataFrame, column: str) -> list[str]:
    return [v.strip() for v in sample[column].drop_nulls().to_list() if v.strip()]


def looks_like_email(values: list[str], threshold: float = 0.8) -> bool:
    if not values:
        return False
    hits = sum(1 for v in values if re.match(EMAIL_RE, v))
    return hits / len(values) >= threshold


def detect_date_format(values: list[str]) -> tuple[str | None, str | None]:
    """(format, note). The format must parse at least 90% of the values.

    If several formats fit (only days <= 12 in the sample), the first in DATE_FORMATS
    is returned with a note asking to confirm it.
    """
    if not values:
        return None, None
    fits = []
    for fmt in DATE_FORMATS:
        ok = 0
        for v in values:
            try:
                datetime.strptime(v, fmt)
                ok += 1
            except ValueError:
                pass
        if ok / len(values) >= 0.9:
            fits.append(fmt)
    if not fits:
        return None, None
    if len(fits) > 1:
        return fits[0], f"ambiguous date format: {' or '.join(fits)}; confirm"
    return fits[0], None


class OfflineMatcher:
    """Name and value matching, no network. The default provider."""

    def suggest(self, sample: pl.DataFrame, entity: Entity) -> list[Suggestion]:
        columns = list(sample.columns)
        pairs: list[tuple[float, str, EntityField, str]] = []
        for f in entity.fields:
            names = [f.name.replace("_", " "), *f.synonyms]
            for col in columns:
                score, why = name_score(col, names)
                pairs.append((score, why, f, col))

        # Values: e-mail-looking columns are strong evidence for an email field.
        for f in entity.fields:
            if f.type != "email":
                continue
            for col in columns:
                if looks_like_email(_values(sample, col)):
                    pairs.append((0.9, "values look like e-mail addresses", f, col))

        # One column per field and one field per column, best pairs first.
        pairs.sort(key=lambda p: p[0], reverse=True)
        chosen: dict[str, Suggestion] = {}
        used: set[str] = set()
        for score, why, f, col in pairs:
            if score < ACCEPT or f.name in chosen or col in used:
                continue
            chosen[f.name] = self._finish(f, col, score, why, sample)
            used.add(col)

        out = []
        for f in entity.fields:
            out.append(chosen.get(f.name) or Suggestion(f.name, None, 0.0, "no matching column"))
        return out

    @staticmethod
    def _finish(
        f: EntityField, col: str, score: float, why: str, sample: pl.DataFrame
    ) -> Suggestion:
        values = _values(sample, col)
        if f.type == "date":
            fmt, note = detect_date_format(values)
            if fmt is None:
                return Suggestion(f.name, col, min(score, 0.5), why,
                                  note="values don't look like dates")  # fmt: skip
            return Suggestion(f.name, col, score, why, date_format=fmt, note=note)
        if f.type == "email" and values and not looks_like_email(values, 0.5):
            return Suggestion(f.name, col, min(score, 0.7), why,
                              note="most values aren't e-mail addresses")  # fmt: skip
        return Suggestion(f.name, col, score, why)


@dataclass(frozen=True)
class ValueSummary:
    values: list[tuple[str, int]]  # most common spelling of each value, with its count
    case_variants: bool  # the same value appears with different capitalization


def distinct_values(sample: pl.DataFrame, column: str, limit: int = 25) -> ValueSummary | None:
    """Distinct values ignoring case, if there are few enough to list."""
    groups: dict[str, dict[str, int]] = {}
    for v in _values(sample, column):
        spellings = groups.setdefault(v.casefold(), {})
        spellings[v] = spellings.get(v, 0) + 1
    if not groups or len(groups) > limit:
        return None
    values = sorted(
        ((max(sp, key=lambda k: sp[k]), sum(sp.values())) for sp in groups.values()),
        key=lambda vc: (-vc[1], vc[0]),
    )
    return ValueSummary(values, any(len(sp) > 1 for sp in groups.values()))
