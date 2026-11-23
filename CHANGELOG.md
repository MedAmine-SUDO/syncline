# Changelog

## Unreleased

- Hooks: `hooks: {after_read, after_map}` run Python functions next to the flow; the engine checks that
  every row is kept and (after mapping) that columns and types are unchanged. `syncline validate` imports them.
  Example: `examples/acme/acme_users_with_hooks.yaml`.

- Raw-file archive: every input file is kept, read-only, by content hash (`--archive`, `SYNCLINE_ARCHIVE`,
  `--no-archive`); runs record the flow file and the archived file.
- `syncline replay RUN_ID`: re-run a past run's archived file through the flow's current configuration;
  the archive copy is verified against its hash first.
- Run histories from 0.1.0 are upgraded in place.
- Default history and archive paths are read from the environment at call time.

- Per-field update policies: `update: always | if_empty | never` on mapping fields.
- Updates write only the fields that changed in each row (grouped by changed set), so protected fields and
  concurrent edits in the application are left alone.

## 0.1.0 (2026-11-23)

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
