# Changelog

## 0.3.0 (2026-11-23)

Sources and targets beyond files and databases.

- REST target (`load.target.type: rest`, extra `syncline[rest]`): any JSON API. Paginated reads (page or
  cursor), creates and updates one record at a time (`PATCH`/`POST` send only what changed, `PUT` keeps
  fields Syncline doesn't manage), field renames, bearer or header auth from the environment.
- Retries for 429 and temporary errors with exponential backoff, jitter and `Retry-After`; POSTs retried only
  with an idempotency key (on by default); client-side rate limit.
- New run status `partial` and exit code 5: writes an API refused are recorded one by one (change log action
  `failed`, report section "Writes the target refused"); 401/403, an API still down after retries, or
  `stop_after_failures` refusals in a row stop sending. The next run completes the rest.
- `examples/fake_api/server.py` (standard library) and `examples/acme/acme_users_api.yaml`.
- Run history: new `failed` column (older histories upgraded in place).

- S3 source (`type: s3`, extra `syncline[s3]`): AWS or S3-compatible stores, standard AWS credential chain,
  last matching key by name, download size checked.
- SFTP source (`type: sftp`, extra `syncline[sftp]`): password or key login, host keys checked against
  `known_hosts` (unknown or changed servers refused), files modified less than `min_age_seconds` ago ignored,
  download size checked.
- Secrets in flow files are refused: passwords and passphrases must be `env:VARIABLE` references.
- Runs record where the file came from (`s3://...`, `sftp://...`); remote files are archived like local ones.
- `source.type` may be left out (defaults to `file`); clearer messages for source errors.
- `HookContext.origin`: where the file came from.

## 0.2.0 (2026-11-23)

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
