"""Acme-specific logic, kept out of the engine.

Acme's export writes job titles with French inclusive-writing markers: "Ingénieur(e)",
"Technicien(ne)", "Chargé(e) de clientèle". The application wants one plain title.
"""

import polars as pl

from syncline.hooks import HookContext


def job_titles(df: pl.DataFrame, ctx: HookContext) -> pl.DataFrame:
    """after_read: add a `Poste (clean)` column without the (e) / (ne) markers."""
    clean = pl.col("Poste").str.replace_all(r"\((e|ne|le|se|ère|euse|rice)\)", "").str.strip_chars()
    return df.with_columns(clean.alias("Poste (clean)"))
