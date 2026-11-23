# How Syncline works, and why it works that way

Syncline is built around one rule: **a sync must never surprise the people who depend on the data.**
Every decision below follows from it.

## A run, step by step

```
1. read      file → text table (encoding and delimiter detected, line numbers kept);
             the file is kept in the archive by its hash
2. skip?     same bytes (sha256) as the last successful run → stop, nothing to do
3. map       rename, transform, translate, default, type; bad cells become reasons
4. contract  required / allowed / unique; too many bad rows → stop
5. diff      valid rows vs target rows, by business key
6. guard     changes too large → stop
7. load      one transaction (skipped by --dry-run)
8. record    run, rejected rows and change log saved to the run history
```

Every run ends with exactly one entry in the history:

| Status | Meaning | Target |
|---|---|---|
| `ok` | changes applied | written |
| `dry_run` | changes computed, recorded as a preview | untouched |
| `skipped` | same file as the last successful run | untouched |
| `rejected` | too many invalid rows | untouched |
| `guarded` | changes too large | untouched |

Problems that aren't about the data (a broken flow file, a missing source file, a missing column,
an unreachable database) raise an error before anything is written.

## Decisions

### Everything is read as text

Type inference on customer files is where data silently goes wrong: `00123` becomes `123`, `1/2` becomes
a date, a column of numbers with one `n/a` becomes all text or all null. Syncline reads every value as text
and applies types from the mapping. A value that doesn't fit is rejected **with a reason and its original
value**, so the customer can fix it at the source.

### Line numbers survive the pipeline

Every row carries its line number in the original file (`__row`) through mapping, checks and diff.
"Row 131: duplicate external_id" is something a customer's HR assistant can act on; "record 129" isn't.

### Duplicates reject every copy

When two rows share a key, one of them is wrong, and the engine can't tell which. Loading the first, the
last or a merge would be a guess presented as data. Both are rejected and reported together
(`rows 16, 131`), and the customer decides.

### Too many bad rows means the wrong file

Rejecting a few rows is normal. Rejecting 40% usually means the wrong export, a changed layout or a
broken encoding. Above `max_rejected_ratio` the run stops before touching the target.

### The diff comes first, and drives everything

Incoming rows are compared with the target by business key before anything is written, giving four sets:
create, update (with the list of changed fields and their old values), deactivate, unchanged.
The dry-run preview, the load, the change log and the report all use **the same diff**, so a preview is
exactly what a real run applies. A test checks it.

### Writes are diff-driven, not upserts

The load is plain `INSERT` for creates and `UPDATE ... WHERE key = ...` for updates and deactivations,
in **one transaction**. Compared with dialect-specific upserts (`ON CONFLICT`, `MERGE`):

- it works on any SQLAlchemy database, with or without a unique constraint on the key;
- unchanged records aren't written at all (no churn on `updated_at` triggers, no replication noise);
- re-running the same file writes nothing: the run is **idempotent** by construction;
- a failure halfway rolls everything back; the target is never half-synced.

### Updates touch only what changed, and only what they're allowed to

Customer files are rarely the only source of truth: people fix a department or add a nickname in the
application. Each mapped field has an update policy (`always`, `if_empty`, `never`) that decides whether a
difference counts as a change at all. Then an update writes **only the changed fields of that row**: rows are
grouped by the set of fields that changed, one `UPDATE` per group. Unchanged fields aren't rewritten with the
values read a moment earlier, so an edit made in the application between the read and the write isn't lost.

### A rejected row never deactivates a record

In full-snapshot mode, records missing from the file are deactivated. A record whose row is in the file but
was rejected (an e-mail wiped by mistake, a typo in a date) is **not** missing: deactivating that person
because of a data-entry error would be the worst possible outcome. Keys of rejected rows count as present.

This was a real bug, found while checking the day-2 demo run, and is now covered by a test.

### Deactivate, never delete

Records leaving the file get the `deactivate.set` values (for example `status: inactive`). Nothing is
deleted, history in the application survives, and a person who comes back is reactivated by the normal diff.

### The guard: big changes need a human

A truncated export (the classic half-written file) looks exactly like "most people left". With
`max_deactivate_ratio: 0.2`, a run that would deactivate more than 20% of the **active** records stops,
writes nothing and exits with code 4. Records that are already inactive don't count, or a truncated file
would look less dangerous than it is. Dry runs never stop; they warn.

### Same file twice is a no-op

The run history stores the file's sha256. If it matches the last successful run, the run is skipped and
recorded as `skipped`. `--force` runs it anyway, and the diff shows nothing to do.

### Every input file is kept, and any run can be replayed

Before anything else happens, the input file is copied to the archive, named by its sha256
(`<archive>/<flow>/<sha256>.csv`): the same file is stored once, nothing is ever overwritten, the copy is
read-only and written atomically (temp file, then rename). The run records where its file was kept.

`syncline replay RUN_ID` runs that exact file again through the flow's **current** configuration, which is
what you want after fixing a mapping or a contract: same bytes, new rules, recorded as a new run
("replay of run …"). The archived file is checked against its hash first; a modified copy is refused.
The customer's original file can be gone by then.

### The history upgrades itself

Newer versions store more per run (the flow file used, the archived file). Opening a history written by an
older version adds the missing columns in place; old runs stay readable and simply can't be replayed.

### The run history lives in its own database

Runs, rejected rows and the change log are kept in a small SQLite file, separate from the target, so the
audit trail survives whatever happens to the target and never mixes with business data. Reports are built
**only** from that history: any past run can be reported again, identically, after the source file is gone.

### Reports are for the customer too

The HTML report starts with "Rows we couldn't load", with the reason and the original values for each row,
written so a non-technical person can fix the next export. Everything from the file is HTML-escaped
(customer data is untrusted input).

### Mapping suggestions are cautious

`syncline map` prefers leaving a field unmapped over guessing: a missing mapping is visible and harmless,
a wrong one loads wrong data. Matches below 0.60 confidence are dropped, below 0.85 they are marked
`REVIEW`, ambiguous date formats (`01/02/2024`) are flagged, and values that differ only by capitalization
are pointed out. The matcher sits behind a `MappingProvider` interface, so another strategy (an LLM, or
mappings learned from past files) can be added without touching the rest.

## Layout

```
src/syncline/
  spec.py            flow file models and validation
  io/read.py         file discovery, encoding/delimiter detection, CSV/Excel as text
  mapping/apply.py   transforms, value maps, defaults, types → typed table + cell errors
  mapping/suggest.py offline matcher (names + values), MappingProvider protocol
  mapping/draft.py   draft flow file with confidences and REVIEW markers
  entities.py        built-in target fields and their synonyms
  contract.py        required / allowed / unique / max ratio
  diff.py            create / update / deactivate / unchanged
  targets/sql.py     table creation, reads, transactional writes
  engine.py          one run, stage by stage, and the guard
  state.py           run history, rejected rows, change log, in-place upgrades
  archive.py         content-addressed, read-only copies of input files
  report/            HTML report (Jinja2, autoescaped) and JSON
  cli.py             commands, exit codes
```
