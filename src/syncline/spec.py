"""Flow file models: what a sync looks like, validated before anything runs.

A flow file describes one sync: where the file comes from, how its columns map to
the entity's fields, what a valid row is, and how rows are loaded into the target.
Unknown keys are rejected, so a typo in a flow file fails loudly instead of being ignored.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

FieldType = Literal["string", "int", "float", "bool", "date", "email"]
TransformName = Literal["trim", "lower", "upper", "title", "collapse_spaces", "digits_only"]
UpdatePolicy = Literal["always", "if_empty", "never"]
HookStage = Literal["after_read", "after_map"]
Scalar = str | int | float | bool

_FLOW_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileSource(_Strict):
    """A local CSV or Excel file.

    `path` is relative to the flow file and may be a glob: the last match by name is used.
    """

    type: Literal["file"] = "file"
    path: str
    encoding: str | None = None  # None = detect (utf-8, utf-8-sig, cp1252)
    delimiter: str | None = None  # None = detect (, ; tab |)
    sheet: str | None = None  # Excel only; None = first sheet


class FieldMapping(_Strict):
    """How one target field is filled from the source file."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source: str | None = Field(default=None, alias="from")
    type: FieldType = "string"
    format: str | None = None  # strptime format, dates only
    transform: list[TransformName] = Field(default_factory=list)
    values: dict[str, Scalar] = Field(default_factory=dict)  # value map, applied after transforms
    default: Scalar | None = None
    # When an existing record may be changed: always (default), if_empty (only fill a
    # blank value in the target), never (set on create only; the application owns it).
    update: UpdatePolicy = "always"

    @model_validator(mode="after")
    def _check(self) -> FieldMapping:
        if self.format is not None and self.type != "date":
            raise ValueError("'format' only applies to fields of type 'date'")
        if self.source is None and self.default is None:
            raise ValueError("needs 'from' (a source column) or a 'default' value")
        return self


class Contract(_Strict):
    """What a valid row is. Invalid rows are quarantined with a reason, not loaded."""

    required: list[str] = Field(default_factory=list)
    unique: list[str] = Field(default_factory=list)
    allowed: dict[str, list[Scalar]] = Field(default_factory=dict)
    max_rejected_ratio: Annotated[float, Field(ge=0, le=1)] = 0.05


class SqlTarget(_Strict):
    type: Literal["sql"] = "sql"
    url: str  # SQLAlchemy URL, or "env:VAR_NAME" to read it from the environment
    table: str


class Deactivate(_Strict):
    """Fields set on target rows whose key is missing from a full snapshot."""

    set: dict[str, Scalar]

    @field_validator("set")
    @classmethod
    def _not_empty(cls, v: dict[str, Scalar]) -> dict[str, Scalar]:
        if not v:
            raise ValueError("'set' needs at least one field, e.g. {status: inactive}")
        return v


class Guard(_Strict):
    """Stop a run whose changes are suspiciously large, before anything is written.

    A file that would deactivate a third of the company is far more often a truncated
    export than a real event. Ratios are relative to the records already in the target.
    Override once with `syncline run --allow-mass-change`.
    """

    max_deactivate_ratio: Annotated[float, Field(ge=0, le=1)] = 0.2
    max_update_ratio: Annotated[float, Field(ge=0, le=1)] | None = None


class Load(_Strict):
    target: SqlTarget
    key: list[str] = Field(min_length=1)
    mode: Literal["full_snapshot", "incremental"] = "full_snapshot"
    deactivate: Deactivate | None = None
    guard: Guard = Field(default_factory=Guard)

    @model_validator(mode="after")
    def _check(self) -> Load:
        if self.mode == "full_snapshot" and self.deactivate is None:
            raise ValueError(
                "mode 'full_snapshot' needs 'deactivate' (what to set on records missing "
                "from the file, e.g. {set: {status: inactive}}); use mode 'incremental' "
                "to leave them untouched"
            )
        if self.mode == "incremental" and self.deactivate is not None:
            raise ValueError("'deactivate' only applies to mode 'full_snapshot'")
        return self


_HOOK_REF = r"^[^:]+\.py:[A-Za-z_][A-Za-z0-9_]*$"


class HookRefs(_Strict):
    """Python functions run at fixed points: `path/to/file.py:function`, relative to the flow."""

    after_read: Annotated[str, Field(pattern=_HOOK_REF)] | None = None
    after_map: Annotated[str, Field(pattern=_HOOK_REF)] | None = None

    def configured(self) -> dict[HookStage, str]:
        stages: tuple[HookStage, ...] = ("after_read", "after_map")
        return {s: ref for s in stages if (ref := getattr(self, s))}


class FlowSpec(_Strict):
    flow: str
    entity: str
    source: FileSource
    mapping: dict[str, FieldMapping] = Field(min_length=1)
    contract: Contract = Field(default_factory=Contract)
    load: Load
    hooks: HookRefs = Field(default_factory=HookRefs)

    @field_validator("flow")
    @classmethod
    def _flow_name(cls, v: str) -> str:
        if not _FLOW_NAME.match(v):
            raise ValueError("use lowercase letters, digits and underscores, e.g. acme_users")
        return v

    @field_validator("mapping", mode="before")
    @classmethod
    def _shorthand(cls, v: Any) -> Any:
        # `email: E-mail` is shorthand for `email: {from: E-mail}`
        if isinstance(v, dict):
            return {k: {"from": m} if isinstance(m, str) else m for k, m in v.items()}
        return v

    @model_validator(mode="after")
    def _fields_exist(self) -> FlowSpec:
        fields = set(self.mapping)
        problems = []
        for where, names in (
            ("contract.required", self.contract.required),
            ("contract.unique", self.contract.unique),
            ("contract.allowed", list(self.contract.allowed)),
            ("load.key", self.load.key),
        ):
            unknown = [n for n in names if n not in fields]
            if unknown:
                problems.append(f"{where}: {', '.join(unknown)} not in mapping")
        if problems:
            raise ValueError("; ".join(problems))
        policy_on_key = [k for k in self.load.key if self.mapping[k].update != "always"]
        if policy_on_key:
            raise ValueError(
                f"key fields can't have an update policy (records are matched on them): "
                f"{', '.join(policy_on_key)}"
            )
        optional_keys = [k for k in self.load.key if k not in self.contract.required]
        if optional_keys:
            raise ValueError(
                f"load.key fields must be in contract.required: {', '.join(optional_keys)}"
            )
        return self


class SpecError(Exception):
    """A flow file that can't be used, with one readable line per problem."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


def load_flow(path: Path) -> FlowSpec:
    """Read and validate a flow file. Raises SpecError with readable problems."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(path, ["file not found"]) from None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise SpecError(path, [f"{where}invalid YAML ({getattr(e, 'problem', e)})"]) from None
    if not isinstance(raw, dict):
        raise SpecError(path, ["expected a mapping at the top level (flow:, source:, ...)"])
    try:
        return FlowSpec.model_validate(raw)
    except ValidationError as e:
        raise SpecError(path, [_describe(err) for err in e.errors()]) from None


def _describe(err: Any) -> str:
    loc = ".".join(str(p) for p in err["loc"])
    msg = str(err["msg"]).removeprefix("Value error, ")
    if err["type"] == "extra_forbidden":
        msg = "unknown key (typo?)"
    return f"{loc}: {msg}" if loc else msg


def json_schema() -> dict[str, Any]:
    """JSON Schema for flow files, for editor autocomplete and validation."""
    return FlowSpec.model_json_schema(by_alias=True)
