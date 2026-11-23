"""Hooks: a customer's own logic, in a Python file next to the flow, without forking the engine.

```yaml
hooks:
  after_read: hooks/acme.py:split_full_name   # the text table, before mapping
  after_map: hooks/acme.py:fix_managers       # the typed table, before the contract
```

A hook is a function `(df: polars.DataFrame, ctx: HookContext) -> polars.DataFrame`.

Rules, checked after every call (breaking one stops the run before anything is written):
- it returns a DataFrame;
- it keeps every row, and adds none (`__row` must hold the same line numbers). A dropped
  row would look absent from a full snapshot and deactivate a real record; to leave rows
  out, let the contract reject them, where it's visible in the report;
- `after_map` keeps the same columns and types (the contract and the target rely on them).
  `after_read` may add, rename or change columns; they're read as text again afterwards.

Hooks run as normal Python with your permissions, like any plugin: only use flow files and
hook files you trust.
"""

from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from syncline.io.read import ROW
from syncline.spec import HookStage

Stage = HookStage
HookFn = Callable[[pl.DataFrame, "HookContext"], pl.DataFrame]


class HookError(Exception):
    """A hook can't be loaded, failed, or broke one of the rules."""


@dataclass(frozen=True)
class HookContext:
    flow: str
    entity: str
    file: Path
    run_id: str
    dry_run: bool
    stage: Stage
    origin: str = ""  # where the file came from: a local path, s3://... or sftp://...


def load_hook(ref: str, base_dir: Path) -> HookFn:
    """`path/to/file.py:function`, path relative to the flow file's folder."""
    file_part, _, func_name = ref.rpartition(":")
    path = (base_dir / file_part).resolve()
    if not path.is_file():
        raise HookError(f"hook file not found: {path}")
    # A unique module name per file, so two flows' hooks/acme.py never collide.
    module_name = "syncline_hook_" + hashlib.sha1(str(path).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover: .py files always have a loader
        raise HookError(f"can't load {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise HookError(f"error while loading {path.name}: {type(e).__name__}: {e}") from e
    fn = getattr(module, func_name, None)
    if not callable(fn):
        raise HookError(f"{path.name} has no function {func_name!r}")
    return fn  # type: ignore[no-any-return]


def call_hook(fn: HookFn, ref: str, df: pl.DataFrame, ctx: HookContext) -> pl.DataFrame:
    try:
        out = fn(df, ctx)
    except Exception as e:
        raise HookError(f"hook {ref} ({ctx.stage}) failed: {type(e).__name__}: {e}") from e
    if not isinstance(out, pl.DataFrame):
        raise HookError(f"hook {ref} must return a polars DataFrame, got {type(out).__name__}")
    if ROW not in out.columns:
        raise HookError(f"hook {ref} dropped the {ROW} column (line numbers); keep it")
    before, after = set(df[ROW].to_list()), set(out[ROW].to_list())
    if before != after or out.height != df.height:
        lost, added = sorted(before - after), sorted(after - before)
        detail = []
        if lost:
            detail.append(f"removed line(s) {_few(lost)}")
        if added or out.height != len(after):
            detail.append("added or duplicated rows")
        raise HookError(
            f"hook {ref} must keep every row and add none ({'; '.join(detail)}). "
            "To leave rows out, let the contract reject them."
        )
    if ctx.stage == "after_map" and out.schema != df.schema:
        raise HookError(
            f"hook {ref} (after_map) must keep the same columns and types: "
            f"expected {_schema(df)}, got {_schema(out)}"
        )
    if ctx.stage == "after_read":
        out = out.with_columns(pl.exclude(ROW).cast(pl.String))
    return out.sort(ROW)


def _few(values: list[int], n: int = 5) -> str:
    shown = ", ".join(str(v) for v in values[:n])
    return shown + (f" and {len(values) - n} more" if len(values) > n else "")


def _schema(df: pl.DataFrame) -> str:
    return ", ".join(f"{c}: {t}" for c, t in df.schema.items() if c != ROW)


@dataclass
class Hooks:
    """The hooks of one flow, loaded once per run."""

    refs: dict[Stage, str]
    fns: dict[Stage, HookFn]

    @classmethod
    def load(cls, refs: dict[Stage, str], base_dir: Path) -> Hooks:
        return cls(refs=refs, fns={stage: load_hook(ref, base_dir) for stage, ref in refs.items()})

    def run(self, stage: Stage, df: pl.DataFrame, ctx: HookContext) -> pl.DataFrame:
        fn = self.fns.get(stage)
        return df if fn is None else call_hook(fn, self.refs[stage], df, ctx)
