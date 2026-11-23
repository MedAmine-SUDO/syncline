# Changelog

## 0.1.0 (unreleased)

First version: files in, SQL out.

- `syncline map`: offline mapping suggestions (names in English and French, e-mail and date detection) written as a draft flow file with confidences and `REVIEW` markers.
- Flow files validated by Pydantic: unknown keys rejected, cross-field checks, JSON Schema export.
- CSV and Excel sources; encoding and delimiter detection; every value read as text; line numbers kept.
- Mapping: transforms, value maps, defaults, typed fields; bad cells become reasons.
- Contracts: required, allowed, unique (every duplicate rejected), maximum rejected ratio.
- Diff before write; `--dry-run` shows exactly what a real run applies.
- SQL target (SQLite and Postgres tested): diff-driven inserts and updates in one transaction; deactivate, never delete; rejected rows never deactivate a record.
- Mass-change guard (`max_deactivate_ratio`, `max_update_ratio`), `--allow-mass-change`.
- Run history with rejected rows and a change log; same file twice is skipped.
- HTML and JSON reports rebuilt from the history; `--json` run summaries; documented exit codes.
