"""Generate the fictional Acme Corp HR exports used by the demo and the tests.

Deterministic (seeded): running it twice produces the same files. Every person is fictional.

The files look like a real HR export from a French-speaking company, problems included:
- French headers, `;` delimiter, Windows-1252 encoding (accents break naive UTF-8 readers)
- dd/mm/yyyy dates, stray spaces, inconsistent case in names and departments
- a few rows with no e-mail, a few duplicated employee IDs, one impossible date

Day 2 is day 1 plus a normal day of HR changes: some people leave, some join,
some change department, and one new broken row appears.

Usage: python scripts/make_examples.py [output_dir]   (default: examples/acme)
"""

from __future__ import annotations

import csv
import random
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path

FIRST = [
    "Amira", "Youssef", "Léa", "Karim", "Sofia", "Omar", "Inès", "Hugo", "Nour", "Adam",
    "Chloé", "Rami", "Emma", "Sélim", "Lina", "Lucas", "Maya", "Ziad", "Jade", "Malik",
    "Hélène", "François", "Zoé", "Rémi", "Gaëlle", "Anaïs", "Mehdi", "Camille", "Yasmine", "Théo",
]  # fmt: skip
LAST = [
    "Haddad", "Martin", "Benali", "Dubois", "Trabelsi", "Bernard", "Mansour", "Petit",
    "Gharbi", "Moreau", "Saidi", "Laurent", "Jaziri", "Girard", "Khelifi", "Roux",
    "Lefèvre", "Fontaine", "Chérif", "Mercier", "Bouaziz", "Lemaître", "Garnier", "Ferchichi",
]  # fmt: skip
# How departments appear in the export: mostly clean, sometimes not.
DEPARTMENTS = ["Ventes", "Ingénierie", "RH", "Finances", "Opérations", "Support", "Marketing"]
HEADERS = ["Matricule", "Nom", "Prénom", "E-mail", "Service", "Date d'entrée", "Poste"]
JOBS = [
    "Assistant(e)", "Chargé(e) de clientèle", "Ingénieur(e)", "Manager", "Analyste",
    "Technicien(ne)",
]  # fmt: skip

N_EMPLOYEES = 320
DOMAIN = "acme.example"


def ascii_slug(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def messy(rng: random.Random, value: str) -> str:
    """Spaces and case the way people type them."""
    r = rng.random()
    if r < 0.06:
        return f" {value} "
    if r < 0.10:
        return value.upper()
    if r < 0.13:
        return value.lower()
    return value


def person(rng: random.Random, n: int) -> dict[str, str]:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    hired = date(2015, 1, 1) + timedelta(days=rng.randint(0, 3900))
    return {
        "Matricule": f"ACM{n:05d}",
        "Nom": messy(rng, last),
        "Prénom": messy(rng, first),
        "E-mail": f"{ascii_slug(first)}.{ascii_slug(last)}{n}@{DOMAIN}",
        "Service": messy(rng, rng.choice(DEPARTMENTS)),
        "Date d'entrée": hired.strftime("%d/%m/%Y"),
        "Poste": rng.choice(JOBS),
    }


def day1(rng: random.Random) -> list[dict[str, str]]:
    rows = [person(rng, n) for n in range(1, N_EMPLOYEES + 1)]
    for i in rng.sample(range(len(rows)), 5):  # forgotten e-mails
        rows[i]["E-mail"] = ""
    for i in rng.sample(range(len(rows)), 3):  # an ID typed twice
        dup = person(rng, 90_000 + i)
        dup["Matricule"] = rows[i]["Matricule"]
        rows.insert(rng.randrange(len(rows)), dup)
    rows[rng.randrange(len(rows))]["Date d'entrée"] = "31/02/2021"  # impossible date
    return rows


def day2(rng: random.Random, previous: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = [dict(r) for r in previous]
    clean = [r for r in rows if r["E-mail"] and r["Date d'entrée"] != "31/02/2021"]
    ids = [r["Matricule"] for r in clean]
    unique_ids = [i for i in ids if ids.count(i) == 1]

    leavers = set(rng.sample(unique_ids, 4))
    rows = [r for r in rows if r["Matricule"] not in leavers]

    movers = rng.sample([i for i in unique_ids if i not in leavers], 2)
    for r in rows:
        if r["Matricule"] in movers:
            current = r["Service"].strip().title()
            r["Service"] = rng.choice([d for d in DEPARTMENTS if d.title() != current])

    for n in range(N_EMPLOYEES + 1, N_EMPLOYEES + 7):  # new hires
        rows.append(person(rng, n))

    broken = person(rng, N_EMPLOYEES + 7)  # a new hire typed in a hurry
    broken["E-mail"] = "not-an-email"
    rows.append(broken)
    return rows


def write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="cp1252", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("examples/acme")
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(2026)
    d1 = day1(rng)
    d2 = day2(rng, d1)
    write(out / "employes_2026-11-01.csv", d1)
    write(out / "employes_2026-11-02.csv", d2)
    print(f"wrote {len(d1)} + {len(d2)} rows to {out}/")


if __name__ == "__main__":
    main()
