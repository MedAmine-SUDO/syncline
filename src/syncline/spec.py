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


class ReadOptions(_Strict):
    """How the file's bytes are read, whatever the source."""

    encoding: str | None = None  # None = detect (utf-8, utf-8-sig, cp1252)
    delimiter: str | None = None  # None = detect (, ; tab |)
    sheet: str | None = None  # Excel only; None = first sheet


class FileSource(ReadOptions):
    """A local CSV or Excel file.

    `path` is relative to the flow file and may be a glob: the last match by name is used.
    """

    type: Literal["file"] = "file"
    path: str


# A secret is never written in a flow file, only the name of the environment variable holding it.
_ENV_REF = r"^env:[A-Za-z_][A-Za-z0-9_]*$"
EnvRef = Annotated[str, Field(pattern=_ENV_REF)]


def _env_only(v: Any) -> Any:
    if isinstance(v, str) and not v.startswith("env:"):
        raise ValueError(
            "secrets don't go in flow files: put it in an environment variable and "
            "write env:VARIABLE_NAME, e.g. password: env:ACME_SFTP_PASSWORD"
        )
    return v


def _remote_pattern(v: str) -> str:
    folder, _, name = v.rstrip().rpartition("/")
    if not name:
        raise ValueError("must end with a file name or pattern, e.g. exports/employes_*.csv")
    if any(c in folder for c in "*?["):
        raise ValueError("only the file name may be a pattern, not the folder")
    return v


class S3Source(ReadOptions):
    """An object in an S3 bucket (or an S3-compatible store: MinIO, Scaleway, OVH...).

    `path` is the object key, whose file name may be a glob (`exports/employes_*.csv`): the
    last match by key is used. Credentials come from the usual AWS chain (environment,
    profile, instance role), never from the flow file.
    """

    type: Literal["s3"]
    bucket: Annotated[str, Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.\-]+$")]
    path: str
    region: str | None = None
    endpoint_url: str | None = None  # S3-compatible stores; may be env:VAR
    profile: str | None = None  # an AWS profile from ~/.aws/config

    _pattern = field_validator("path")(_remote_pattern)


class SftpSource(ReadOptions):
    """A file on an SFTP server.

    `path` is the remote path, whose file name may be a glob (`/outgoing/employes_*.csv`).
    The server's host key must be in `known_hosts` (default ~/.ssh/known_hosts): an unknown
    server is refused, never trusted on first use. Files modified less than `min_age_seconds`
    ago are ignored, so a file still being uploaded is never read half-written.
    """

    type: Literal["sftp"]
    host: Annotated[str, Field(min_length=1)]
    port: Annotated[int, Field(ge=1, le=65535)] = 22
    username: Annotated[str, Field(min_length=1)]  # or env:VAR
    password: EnvRef | None = None
    key_file: str | None = None  # private key; ~ expanded, relative to the flow file
    key_passphrase: EnvRef | None = None
    known_hosts: str | None = None
    path: str
    min_age_seconds: Annotated[int, Field(ge=0)] = 60
    timeout_seconds: Annotated[float, Field(gt=0)] = 30

    _pattern = field_validator("path")(_remote_pattern)

    @field_validator("password", "key_passphrase", mode="before")
    @classmethod
    def _no_plain_secret(cls, v: Any) -> Any:
        return _env_only(v)


Source = Annotated[FileSource | S3Source | SftpSource, Field(discriminator="type")]
_SOURCE_TYPES = ("file", "s3", "sftp")


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


class Lifecycle(_Strict):
    """Which values a status-like field may take, and which changes between them are allowed.

    ```yaml
    lifecycle:
      field: status
      initial: [pending, active]          # what a new record may start as
      transitions:
        pending: [active, cancelled]
        active: [suspended, inactive]
        suspended: [active, inactive]
        inactive: [active]                 # someone who comes back
    ```
    A row asking for a change that isn't listed is rejected with a reason; nothing of it is
    written. Every state must appear as a key of `transitions` (with `[]` for a final state).
    """

    field: str
    initial: list[Scalar] | None = None  # None: a new record may start in any state
    transitions: dict[str, list[Scalar]] = Field(min_length=1)

    def states(self) -> list[str]:
        return list(self.transitions)

    def allowed_from(self, state: str) -> list[str]:
        return [str(s) for s in self.transitions.get(state, [])]

    @model_validator(mode="after")
    def _known_states(self) -> Lifecycle:
        states = set(self.transitions)
        unknown = sorted(
            {str(t) for targets in self.transitions.values() for t in targets} - states
            | {str(i) for i in self.initial or []} - states
        )
        if unknown:
            raise ValueError(
                f"unknown state(s) {', '.join(unknown)}: every state needs its own entry in "
                "transitions (use [] for a final state)"
            )
        return self


class SqlTarget(_Strict):
    type: Literal["sql"] = "sql"
    url: str  # SQLAlchemy URL, or "env:VAR_NAME" to read it from the environment
    table: str


_PLACEHOLDER = re.compile(r"{([^{}]*)}")


class RestAuth(_Strict):
    """How requests are authenticated. Secrets are env:VARIABLE references only."""

    bearer: EnvRef | None = None  # Authorization: Bearer <token>
    header: str | None = None  # a custom header, e.g. X-Api-Key ...
    value: EnvRef | None = None  # ... and its value

    @model_validator(mode="after")
    def _one_style(self) -> RestAuth:
        if self.bearer and (self.header or self.value):
            raise ValueError("use either bearer, or header + value, not both")
        if bool(self.header) != bool(self.value):
            raise ValueError("header and value go together")
        return self

    @field_validator("bearer", "value", mode="before")
    @classmethod
    def _no_plain_secret(cls, v: Any) -> Any:
        return _env_only(v)


class Endpoint(_Strict):
    method: Literal["POST", "PUT", "PATCH"]
    path: str  # relative to base_url; {field} placeholders take the record's key values


class Pagination(_Strict):
    """How the list endpoint pages. `page`: page numbers; `cursor`: a token from each response."""

    type: Literal["none", "page", "cursor"] = "none"
    page_param: str = "page"
    size_param: str = "page_size"
    size: Annotated[int, Field(ge=1, le=10_000)] = 100
    first_page: Annotated[int, Field(ge=0)] = 1
    cursor_param: str = "cursor"
    next_cursor: str = "next_cursor"  # where the next token is in the response (dotted path)


class RestTarget(_Strict):
    """A JSON HTTP API: the current records are listed, then created/updated one by one."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    type: Literal["rest"]
    base_url: str  # https://... or env:VAR
    auth: RestAuth = Field(default_factory=RestAuth)
    list_path: str = Field(alias="list")  # GET path returning the current records
    items: str = ""  # where the records are in the list response (dotted path); "" = the body
    pagination: Pagination = Field(default_factory=Pagination)
    create: Endpoint
    update: Endpoint  # also used for deactivations
    fields: dict[str, str] = Field(default_factory=dict)  # target field -> JSON name, if different
    idempotency_header: str | None = "Idempotency-Key"  # sent with every write; null to turn off
    max_requests_per_second: Annotated[float, Field(gt=0)] = 10
    max_retries: Annotated[int, Field(ge=0, le=10)] = 5
    timeout_seconds: Annotated[float, Field(gt=0)] = 30
    stop_after_failures: Annotated[int, Field(ge=1)] = 10  # consecutive refused writes

    def placeholders(self, endpoint: Endpoint) -> list[str]:
        return _PLACEHOLDER.findall(endpoint.path)


Target = Annotated[SqlTarget | RestTarget, Field(discriminator="type")]
_TARGET_TYPES = ("sql", "rest")


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
    target: Target
    key: list[str] = Field(min_length=1)
    mode: Literal["full_snapshot", "incremental"] = "full_snapshot"
    deactivate: Deactivate | None = None
    guard: Guard = Field(default_factory=Guard)

    @field_validator("target", mode="before")
    @classmethod
    def _default_type(cls, v: Any) -> Any:
        if isinstance(v, dict) and "type" not in v:
            return {"type": "sql", **v}
        return v

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
    source: Source
    mapping: dict[str, FieldMapping] = Field(min_length=1)
    contract: Contract = Field(default_factory=Contract)
    load: Load
    hooks: HookRefs = Field(default_factory=HookRefs)
    lifecycle: Lifecycle | None = None

    @field_validator("flow")
    @classmethod
    def _flow_name(cls, v: str) -> str:
        if not _FLOW_NAME.match(v):
            raise ValueError("use lowercase letters, digits and underscores, e.g. acme_users")
        return v

    @field_validator("source", mode="before")
    @classmethod
    def _default_type(cls, v: Any) -> Any:
        # `type: file` may be left out
        if isinstance(v, dict) and "type" not in v:
            return {"type": "file", **v}
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
        if self.lifecycle:
            lc = self.lifecycle
            if lc.field not in fields:
                raise ValueError(f"lifecycle.field: {lc.field} not in mapping")
            if lc.field in self.load.key:
                raise ValueError("lifecycle.field can't be a key field")
            deactivate = self.load.deactivate.set if self.load.deactivate else {}
            if lc.field in deactivate and str(deactivate[lc.field]) not in lc.states():
                raise ValueError(
                    f"load.deactivate.set.{lc.field}: {deactivate[lc.field]!r} is not a "
                    f"lifecycle state ({', '.join(lc.states())})"
                )
        target = self.load.target
        if isinstance(target, RestTarget):
            for name, endpoint in (("create", target.create), ("update", target.update)):
                bad = [p for p in target.placeholders(endpoint) if p not in self.load.key]
                if bad:
                    raise ValueError(
                        f"load.target.{name}.path: {{{bad[0]}}} is not a key field "
                        f"(only {', '.join(self.load.key)} can be used in paths)"
                    )
            if not target.placeholders(target.update):
                raise ValueError(
                    "load.target.update.path must name the record, e.g. /v1/users/{external_id}"
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
    parts = [str(p) for p in err["loc"]]
    if parts[:1] == ["source"] and len(parts) > 1 and parts[1] in _SOURCE_TYPES:
        del parts[1]  # pydantic adds the union tag: source.sftp.password → source.password
    if parts[:2] == ["load", "target"] and len(parts) > 2 and parts[2] in _TARGET_TYPES:
        del parts[2]
    loc = ".".join(parts)
    msg = str(err["msg"]).removeprefix("Value error, ")
    if err["type"] == "extra_forbidden":
        msg = "unknown key (typo?)"
    elif err["type"] == "union_tag_invalid":
        types = _TARGET_TYPES if parts[:2] == ["load", "target"] else _SOURCE_TYPES
        msg = f"unknown type {err['input'].get('type')!r}; use one of: {', '.join(types)}"
    return f"{loc}: {msg}" if loc else msg


def json_schema() -> dict[str, Any]:
    """JSON Schema for flow files, for editor autocomplete and validation."""
    return FlowSpec.model_json_schema(by_alias=True)
