# How Syncline works, and why it works that way

Syncline is built around one rule: **a sync must never surprise the people who depend on the data.**
Every decision below follows from it.

## A run, step by step

```
1. read      fetch the file (folder, S3 or SFTP) → text table (encoding and delimiter
             detected, line numbers kept); the file is kept in the archive by its hash
2. skip?     same bytes (sha256) as the last successful run → stop, nothing to do
3. map       hook after_read → rename, transform, translate, default, type → lookups into
             other flows' targets → hook after_map; bad cells become reasons
4. contract  required / allowed / unique; too many bad rows → stop
5. diff      valid rows vs target rows, by business key; lifecycle rules checked
             (forbidden status changes rejected, like contract failures)
6. guard     changes too large → stop
7. load      one transaction (database) or record by record with retries (API);
             skipped by --dry-run
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
| `partial` | an API target refused some writes; each is recorded | partly written |

Problems that aren't about the data (a broken flow file, a missing source file, a missing column,
an unreachable database) raise an error before anything is written.

## Decisions

### Files come from where customers put them, and every source behaves the same

Customers drop exports in a folder, a bucket or on an SFTP server. Each source only has to list candidate
files and download one into the run's temporary folder; from there the run is identical (read, archive,
skip check, map...), so a remote file gets exactly the same guarantees as a local one, including replay
after the original is gone. The choice is the **last file by name**, never by modification time: dated
names sort correctly, while timestamps change on every copy and upload.

Remote sources add their own ways to go wrong, each handled before any processing:

- **A file still being uploaded.** SFTP uploads aren't atomic: a file can be listed while half-written.
  Files modified less than `min_age_seconds` ago (60 by default) are ignored; if they are the only
  match, the run says so instead of reading an older file without warning. (S3 objects appear only
  once fully uploaded, so S3 doesn't need this.)
- **An incomplete download.** The downloaded size is compared with the size the server listed.
- **The wrong server.** SFTP host keys are checked against `known_hosts`, and an unknown or changed key is
  refused: trusting a server on first use is how files end up stolen or swapped.
- **Leaked credentials.** Flow files are configuration, meant to be committed and reviewed; secrets are
  `env:` references, and a plain password is a validation error. S3 uses the standard AWS credential chain,
  so a read-only role works without any secret at all.

The S3 and SFTP clients (boto3, paramiko) are optional extras, imported only when a flow uses them.

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

### An API has no transaction, so runs are made to converge

A database applies the whole diff or nothing. An HTTP API applies one record at a time and can refuse any of
them, so the REST target is built to be safe without a transaction:

- **It reads before it writes.** Every page of the current records is listed first; only real differences
  are sent. A run that stopped halfway is completed by the next one, which sees what the API holds by then:
  the system converges instead of duplicating.
- **Retries can't create duplicates.** 429 and temporary failures are retried with exponential backoff and
  jitter, and `Retry-After` is honoured. A request that never left (connection refused) is always safe to
  retry; one that may have been processed (timeout, lost reply) is retried only if it is idempotent:
  `PATCH`/`PUT`, or a `POST` carrying an idempotency key that is stable across retries. Otherwise the run
  stops and says so, rather than guessing.
- **One bad record doesn't block the others.** A refused write is recorded with the API's own message; the
  run is `partial` (exit code 5) and the report lists each one. The change log only contains what was
  really applied.
- **Systematic failure stops early.** Bad credentials, an API that stays down, or ten refusals in a row
  stop sending: hundreds of identical errors help nobody, and the API's owners will thank you.
- **Our own rate limit** keeps the API from having to defend itself.

The fake API used by the tests (`examples/fake_api/server.py`, standard library only) speaks real HTTP and can
inject 429s, 5xx, lost replies and refused records, so each of these paths is tested end to end.

### Flows can look each other up, without becoming one big job

Real data refers to other data: participants point at people and trainings. Instead of a flow that loads
everything at once, each kind of record keeps its own flow, file, contract and history, and a field can take
its value from another flow's **target**: the records as they really are after that flow's runs, not its input
file. A value that matches nothing, or several records, rejects the row with a reason: guessing which
"Blue team" was meant is how data quietly goes wrong. `syncline run-all` orders flows by their lookups and
doesn't run a flow whose dependency failed, because its lookups would read incomplete data.

### Status changes follow a lifecycle

A status is not just a value: some changes make no sense (an archived training published again, a draft
course marked as completed), and a file asking for one is usually wrong. The contract can't see that, because
it only sees the file; the lifecycle check runs on the diff, where the current value and the new one meet.
A forbidden change rejects the row as a whole, with a reason the customer can act on, and counts towards
the rejected ratio: a file full of impossible transitions is as suspicious as a file full of bad e-mails.
Deactivation is a status change too, so it obeys the same rules: the engine never forces a record into a
state the lifecycle forbids, it leaves it and says so.

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

### Custom logic goes in hooks, with rules the engine enforces

Some customers need logic no configuration covers. Instead of forking the engine (the road back to one
script per customer), a flow can name Python functions to run on the text table after reading and on the
typed table after mapping. The engine checks what comes back: a hook may change values, but it can't make
rows disappear (that would deactivate people in a full snapshot) or change the types the contract and the
target rely on. A failing hook stops the run before anything is written.

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
  io/fetch.py        getting the input file: folder, S3 or SFTP, into a temporary folder
  io/read.py         file discovery, encoding/delimiter detection, CSV/Excel as text
  mapping/apply.py   transforms, value maps, defaults, types → typed table + cell errors
  mapping/suggest.py offline matcher (names + values), MappingProvider protocol
  mapping/draft.py   draft flow file with confidences and REVIEW markers
  entities.py        built-in target fields and their synonyms
  contract.py        required / allowed / unique / max ratio
  lifecycle.py       allowed states and transitions, checked on the diff
  lookups.py         values from other flows' targets, and the run order of several flows
  diff.py            create / update / deactivate / unchanged
  targets/base.py    what a target does (read, apply, report failures)
  targets/sql.py     table creation, reads, transactional writes
  targets/rest.py    paginated reads, retried/rate-limited/idempotent writes
  engine.py          one run, stage by stage, the guard, and replay
  hooks.py           loading and calling hooks, and the rules they must follow
  state.py           run history, rejected rows, change log, in-place upgrades
  archive.py         content-addressed, read-only copies of input files
  report/            HTML report (Jinja2, autoescaped) and JSON
  cli.py             commands, exit codes
```
