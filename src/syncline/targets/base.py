"""What every target does: read the current records, apply a diff, say what failed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import polars as pl

    from syncline.diff import Diff
    from syncline.spec import FieldMapping, Scalar, Target


@dataclass(frozen=True)
class WriteFailure:
    action: str  # create | update | deactivate
    key: dict[str, Any]
    error: str
    status: int | None = None  # HTTP status, when there was one


@dataclass
class ApplyResult:
    """What a target did with a diff. SQL applies everything or nothing; an API can't."""

    failed: list[WriteFailure] = field(default_factory=list)
    stopped: str | None = None  # why the remaining writes weren't sent, if they weren't

    def failed_count(self, action: str) -> int:
        return sum(1 for f in self.failed if f.action == action)


class TargetHandle(Protocol):
    def read(self, key: list[str]) -> pl.DataFrame: ...

    def apply(self, diff: Diff) -> ApplyResult: ...

    def columns(self, names: list[str]) -> pl.DataFrame:
        """Any columns of the target (managed or not), as text: for lookups from other flows."""
        ...


def open_target(
    target: Target,
    mapping: dict[str, FieldMapping],
    key: list[str],
    *,
    extra: dict[str, Scalar] | None,
    create: bool,
    run_id: str,
) -> TargetHandle:
    from syncline.spec import RestTarget

    if isinstance(target, RestTarget):
        from syncline.targets.rest import RestApi

        return RestApi.open(target, mapping, key, extra=extra, run_id=run_id)
    from syncline.targets.sql import SqlTable

    return SqlTable.open(target, mapping, key, extra=extra, create=create)
