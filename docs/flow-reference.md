# Flow file reference

A flow file describes one sync: where the data comes from, how its columns become fields,
what a valid row is, and how rows are loaded. It is YAML, validated before anything runs.

- **Unknown keys are errors.** A typo like `transfrom:` fails `syncline validate` instead of being ignored.
- **Every problem is listed at once**, with its location: `mapping.email.transfrom: unknown key (typo?)`.
- **Editor autocomplete:** `syncline schema > flow.schema.json`, then point your editor's YAML plugin at it.

```yaml
flow: acme_users            # required
entity: user                # required: a label for what is synced
source: { ... }             # required
mapping: { ... }            # required, at least one field
contract: { ... }           # optional (defaults below)
load: { ... }               # required
```

## `flow`

The flow's name: lowercase letters, digits and underscores, starting with a letter (`acme_users`).
It identifies the flow in the run history, so renaming a flow starts a new history.

## `source`

| Key | Default | Meaning |
|---|---|---|
| `type` | `file` | The only source type in this version. |
| `path` | required | Path to the file, **relative to the flow file**. May be a glob (`employes_*.csv`): the **last match by name** is used, so dated names (`2026-11-02`) pick the newest export. Modification times are not used: they change on copy and checkout. |
| `encoding` | detected | Force an encoding (`utf-8`, `cp1252`, ...). Detection tries UTF-8 with or without BOM, then Windows-1252. |
| `delimiter` | detected | Force a CSV delimiter. Detection considers `,` `;` tab `\|`. |
| `sheet` | first sheet | Excel only: the sheet name. |

`syncline run --file other.csv` overrides `path` for one run.

Files ending in `.xlsx`, `.xlsm` or `.xls` are read as Excel (install `syncline[excel]`); anything else as CSV.

**Every column is read as text.** Types are applied by the mapping, so a value that doesn't fit its type
becomes a rejected row with a reason, never a crash or a silently wrong number. Leading zeros survive.
Headers are trimmed; empty or duplicate headers are an error. Blank and whitespace-only cells are empty.

## `mapping`

One entry per target field. The field name is the column name in the target.

```yaml
mapping:
  email: E-mail                                   # shorthand for { from: E-mail }
  hire_date: { from: "Date d'entrée", type: date, format: "%d/%m/%Y" }
  department:
    from: Service
    transform: [trim, lower]
    values: { ventes: Sales, rh: HR }
    default: Unassigned
  status: { default: active }                     # a constant: no source column
```

| Key | Default | Meaning |
|---|---|---|
| `from` | none | Source column (header as it appears in the file, trimmed). |
| `type` | `string` | `string`, `email`, `int`, `float`, `bool`, `date`. |
| `format` | `%Y-%m-%d` | `date` only: a [strptime](https://docs.python.org/3/library/datetime.html#format-codes) format. |
| `transform` | `[]` | Applied in order: `trim`, `lower`, `upper`, `title`, `collapse_spaces`, `digits_only`. |
| `values` | `{}` | Value map, applied after the transforms: `{old: new}`. Values not listed are kept. |
| `default` | none | Used when the cell is empty, or always when there is no `from`. |

A field needs `from` or `default` (or both). A mapped column missing from the file stops the run
before anything is read further, with the list of columns the file does have.

**Order of operations per cell:** read as text → transforms → empty text becomes empty → value map → default → type.

**Types:**

| Type | Accepts | Rejects with |
|---|---|---|
| `string` | anything | |
| `email` | `name@domain.tld` | `not a valid e-mail address` |
| `int` | `1200`, `1 200` (spaces removed) | `not a whole number` |
| `float` | `3.5`, `3,5` (French decimal comma) | `not a number` |
| `bool` | `true/false`, `yes/no`, `y/n`, `1/0`, `oui/non`, `o/n`, `vrai/faux` (any case) | `not yes/no` |
| `date` | the `format` | `not a valid date (dd/mm/yyyy)`; impossible dates like 31/02 included |

## `contract`

What a loadable row is. Rows that break it are **rejected**: not loaded, stored with their reasons
and their original values, and listed in the report.

| Key | Default | Meaning |
|---|---|---|
| `required` | `[]` | Fields that must not be empty. |
| `unique` | `[]` | Fields whose values must not repeat. **Every** copy of a duplicate is rejected: the engine can't know which one is right, and loading either would be a guess. |
| `allowed` | `{}` | `{field: [values]}`: anything else is rejected. |
| `max_rejected_ratio` | `0.05` | If a larger share of rows is rejected, **the whole run stops** and nothing is loaded (exit code 2): a file that is mostly wrong is usually the wrong file. |

Every field named here must be in `mapping`. Key fields must be `required`.

## `load`

| Key | Default | Meaning |
|---|---|---|
| `target` | required | `{type: sql, url: ..., table: ...}`. `url` is a [SQLAlchemy URL](https://docs.sqlalchemy.org/en/20/core/engines.html#database-urls), or `env:VAR` to read it from an environment variable (keep credentials out of flow files). |
| `key` | required | The business key: one or more fields identifying a record (`[external_id]`). |
| `mode` | `full_snapshot` | `full_snapshot`: the file is the complete list, so records missing from it are deactivated. `incremental`: the file only contains changes; missing records are left alone. |
| `deactivate` | none | `full_snapshot` only, and required there: `{set: {status: inactive}}`. Records are never deleted. |
| `guard` | see below | Limits that stop a run whose changes look suspiciously large. |

**Guard** (ratios of the *active* records already in the target; first loads are never guarded):

| Key | Default | Meaning |
|---|---|---|
| `max_deactivate_ratio` | `0.2` | Stop if the run would deactivate more than this share. |
| `max_update_ratio` | none | Stop if the run would update more than this share. |

A stopped run writes nothing and exits with code 4. If the change is real, run again with
`--allow-mass-change`. A dry run never stops; it warns that a real run would.

**Target table.** If it doesn't exist, it is created from the mapping (types mapped to SQL types, the key as
primary key). If it exists, it must have every mapped column; other columns are left alone.
A record that comes back after being deactivated is reactivated by the normal diff, as long as the flow
sets the field back (for example `status: { default: active }`).
