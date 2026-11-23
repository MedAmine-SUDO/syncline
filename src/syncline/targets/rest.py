"""REST target: any JSON HTTP API that can list its records and create/update one at a time.

An API has no transaction, so this target is built to be safe without one:

- **Read first, write the diff.** The current records are listed (all pages), so only real
  changes are sent, and a run that fails halfway is completed by the next one: the diff is
  computed against what the API holds by then. Runs converge instead of duplicating.
- **Retries that can't duplicate.** 429 and temporary errors (5xx, timeouts) are retried
  with exponential backoff and jitter, honouring Retry-After. A POST is only retried when it
  carries an idempotency key (one per run, action and record, stable across retries), or
  when the request provably never left (connection refused).
- **Per-record failures are results.** A refused record (400, 409, 422...) is recorded with
  the API's message and the others continue; the run ends `partial`. Systematic failure
  (401/403, the API still down after retries, N refusals in a row) stops sending at once.
- **Rate limited** on our side (`max_requests_per_second`), before the API has to say so.
"""

from __future__ import annotations

import email.utils
import json
import random
import time
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import polars as pl

from syncline.diff import CHANGED, Diff
from syncline.io.read import ROW
from syncline.spec import Endpoint, FieldMapping, FieldType, RestTarget, Scalar
from syncline.targets.base import ApplyResult, WriteFailure
from syncline.targets.sql import _PL_TYPES, TargetError

if TYPE_CHECKING:
    import httpx

_RETRY_STATUS = {429, 500, 502, 503, 504}
_FATAL_STATUS = {401, 403}
_MAX_WAIT = 120.0  # seconds: the longest Retry-After we honour, and the backoff cap
_MAX_PAGES = 100_000


def _sleep(seconds: float) -> None:  # a seam for tests
    time.sleep(seconds)


class _Stop(Exception):
    """Stop sending writes: the problem isn't with one record."""


@dataclass
class _Client:
    spec: RestTarget
    http: httpx.Client
    interval: float
    last: float = 0.0
    requests: int = 0

    def request(
        self, method: str, path: str, *, idempotent: bool, **kw: Any
    ) -> httpx.Response | str:
        """The response, or a string saying why there is none (after retries)."""
        import httpx

        attempt = 0
        while True:
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                _sleep(wait)
            self.last = time.monotonic()
            self.requests += 1
            try:
                response = self.http.request(method, path, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                problem: str = f"can't connect: {e}"  # never sent: always safe to retry
                retry = True
            except httpx.TransportError as e:
                problem = f"{type(e).__name__}: {e}"  # may have been processed
                retry = idempotent
            else:
                if response.status_code not in _RETRY_STATUS:
                    return response
                problem = f"HTTP {response.status_code}"
                retry = idempotent or response.status_code == 429  # 429 = not processed
                if retry and attempt < self.spec.max_retries:
                    after = _retry_after(response.headers.get("Retry-After"))
                    if after is not None:
                        attempt += 1
                        _sleep(after)
                        continue
            if not retry or attempt >= self.spec.max_retries:
                return problem if attempt == 0 else f"{problem} after {attempt} retries"
            _sleep(min(_MAX_WAIT, 0.5 * 2**attempt) * (0.5 + random.random() / 2))
            attempt += 1


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - time.time()
    return max(0.0, min(_MAX_WAIT, seconds))


def _dig(data: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for k in ("error", "message", "detail", "errors"):
            if body.get(k):
                text = body[k] if isinstance(body[k], str) else json.dumps(body[k])
                return f"HTTP {response.status_code}: {text[:300]}"
    return f"HTTP {response.status_code}: {response.text[:300]}".rstrip(": ")


def _from_json(value: Any, kind: FieldType | None) -> Any:
    """A JSON value as the type the mapping produces (None = a deactivation field, as text)."""
    if value is None:
        return None
    if kind is None or kind in ("string", "email"):
        return str(value)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise ValueError
    if kind == "int":
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError
        return int(value)
    if kind == "float":
        if isinstance(value, bool):
            raise ValueError
        return float(value)
    return date.fromisoformat(str(value)[:10])


def _to_json(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


@dataclass
class RestApi:
    spec: RestTarget
    client: _Client
    mapping: dict[str, FieldMapping]
    schema: dict[str, Any]
    run_id: str
    current: dict[tuple[str, ...], dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def open(
        cls,
        spec: RestTarget,
        mapping: dict[str, FieldMapping],
        key: list[str],
        *,
        extra: dict[str, Scalar] | None,
        run_id: str,
        transport: httpx.BaseTransport | None = None,
    ) -> RestApi:
        try:
            import httpx
        except ImportError:  # pragma: no cover: the rest extra is installed in tests
            raise TargetError(
                "REST targets need the rest extra: pip install 'syncline[rest]'"
            ) from None
        from syncline.io.fetch import resolve_env
        from syncline.io.read import SourceError

        try:
            base = resolve_env(spec.base_url, "target base_url").rstrip("/")
            headers = {"Accept": "application/json", "User-Agent": "syncline"}
            if spec.auth.bearer:
                headers["Authorization"] = "Bearer " + resolve_env(spec.auth.bearer, "auth.bearer")
            if spec.auth.header and spec.auth.value:
                headers[spec.auth.header] = resolve_env(spec.auth.value, "auth.value")
        except SourceError as e:
            raise TargetError(str(e)) from None
        http = httpx.Client(
            base_url=base, headers=headers, timeout=spec.timeout_seconds, transport=transport
        )
        schema: dict[str, Any] = {f: _PL_TYPES[m.type] for f, m in mapping.items()}
        for f in extra or {}:
            schema.setdefault(f, pl.String)
        client = _Client(spec, http, interval=1 / spec.max_requests_per_second)
        return cls(spec=spec, client=client, mapping=mapping, schema=schema, run_id=run_id)

    def _json_name(self, f: str) -> str:
        return self.spec.fields.get(f, f)

    # --- read -------------------------------------------------------------------------

    def read(self, key: list[str]) -> pl.DataFrame:
        """Every current record (all pages), typed like the incoming frame."""
        records: list[dict[str, Any]] = []
        for item in self._list():
            if not isinstance(item, dict):
                raise TargetError(f"{self.spec.list_path} returned a non-object item: {item!r:.80}")
            ident = tuple(str(item.get(self._json_name(k))) for k in key)
            if any(item.get(self._json_name(k)) is None for k in key):
                raise TargetError(
                    f"a record from {self.spec.list_path} has no {', '.join(key)}: {item!r:.120}"
                )
            if ident in self.current:
                raise TargetError(
                    f"the API holds two records with {'/'.join(key)} = {'/'.join(ident)}; "
                    "fix that first (the diff would be ambiguous)"
                )
            self.current[ident] = item
            row = {}
            for f in self.schema:
                kind = self.mapping[f].type if f in self.mapping else None
                raw = item.get(self._json_name(f))
                try:
                    row[f] = _from_json(raw, kind)
                except (ValueError, TypeError):
                    raise TargetError(
                        f"the API returned {raw!r} for {f} ({kind}) on record {'/'.join(ident)}"
                    ) from None
            records.append(row)
        return pl.DataFrame(records, schema=self.schema, orient="row").sort(key)

    def _get(self, params: dict[str, Any]) -> Any:
        r = self.client.request("GET", self.spec.list_path, idempotent=True, params=params)
        if isinstance(r, str):
            raise TargetError(f"can't list {self.spec.list_path}: {r}")
        if r.status_code >= 400:
            raise TargetError(f"can't list {self.spec.list_path}: {_message(r)}")
        try:
            return r.json()
        except ValueError:
            raise TargetError(f"{self.spec.list_path} didn't return JSON") from None

    def _items(self, body: Any) -> list[Any]:
        items = _dig(body, self.spec.items) if self.spec.items else body
        if not isinstance(items, list):
            where = f"at '{self.spec.items}'" if self.spec.items else "as the body"
            raise TargetError(f"{self.spec.list_path}: expected a list of records {where}")
        return items

    def _list(self) -> list[Any]:
        p = self.spec.pagination
        if p.type == "none":
            return self._items(self._get({}))
        out: list[Any] = []
        page, cursor, seen = p.first_page, None, set()
        previous_first: Any = object()
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {p.size_param: p.size}
            if p.type == "page":
                params[p.page_param] = page
            elif cursor is not None:
                params[p.cursor_param] = cursor
            body = self._get(params)
            items = self._items(body)
            if items and items[0] == previous_first:
                raise TargetError(
                    f"{self.spec.list_path}: pagination doesn't advance (same page twice)"
                )
            previous_first = items[0] if items else None
            out.extend(items)
            if p.type == "page":
                if len(items) < p.size:
                    return out
                page += 1
            else:
                cursor = _dig(body, p.next_cursor)
                if not cursor or not items:
                    return out
                if cursor in seen:
                    raise TargetError(f"{self.spec.list_path}: the cursor repeats ({cursor!r:.40})")
                seen.add(cursor)
        raise TargetError(
            f"{self.spec.list_path}: more than {_MAX_PAGES} pages"
        )  # pragma: no cover

    # --- write ------------------------------------------------------------------------

    def apply(self, diff: Diff) -> ApplyResult:
        """Send creates, then updates, then deactivations; record what the API refused."""
        result = ApplyResult()
        key = diff.key
        writes: list[tuple[str, dict[str, Any], Endpoint, dict[str, Any]]] = []
        for row in diff.create.drop(ROW).iter_rows(named=True):
            body = {self._json_name(f): _to_json(v) for f, v in row.items() if f in self.schema}
            writes.append(("create", row, self.spec.create, body))
        for row in diff.update.iter_rows(named=True):
            fields = row[CHANGED] if self.spec.update.method != "PUT" else list(self.mapping)
            writes.append(("update", row, self.spec.update, self._body(key, row, fields)))
        for row in diff.deactivate.iter_rows(named=True):
            values = {**row, **diff.deactivate_set}
            writes.append(
                ("deactivate", row, self.spec.update,
                 self._body(key, values, list(diff.deactivate_set))),
            )  # fmt: skip

        in_a_row = 0
        for i, (action, row, endpoint, body) in enumerate(writes):
            ident = {k: row[k] for k in key}
            try:
                error = self._send(action, ident, endpoint, body)
            except _Stop as e:
                result.stopped = str(e)
                result.failed.append(WriteFailure(action, ident, str(e)))
                for later_action, later_row, _, _ in writes[i + 1 :]:
                    later = {k: later_row[k] for k in key}
                    result.failed.append(WriteFailure(later_action, later, "not sent: run stopped"))
                break
            if error is None:
                in_a_row = 0
                continue
            result.failed.append(error)
            in_a_row += 1
            if in_a_row >= self.spec.stop_after_failures and i + 1 < len(writes):
                stop = f"stopped after {in_a_row} refused writes in a row (looks systematic)"
                result.stopped = stop
                for later_action, later_row, _, _ in writes[i + 1 :]:
                    later = {k: later_row[k] for k in key}
                    result.failed.append(WriteFailure(later_action, later, "not sent: run stopped"))
                break
        return result

    def _body(self, key: list[str], row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        """PATCH/POST: the given fields. PUT: the record as the API returned it, overlaid."""
        body = {self._json_name(f): _to_json(row[f]) for f in fields if f in self.schema}
        if self.spec.update.method == "PUT":
            ident = tuple(str(row[k]) for k in key)
            return {**self.current.get(ident, {}), **body}
        return body

    def _send(
        self, action: str, ident: dict[str, Any], endpoint: Endpoint, body: dict[str, Any]
    ) -> WriteFailure | None:
        path = endpoint.path.format_map({k: quote(str(v), safe="") for k, v in ident.items()})
        headers = {}
        if self.spec.idempotency_header:
            # Stable for this run, action and record: a retried POST is recognized, not doubled.
            label = "/".join(str(v) for v in ident.values())
            headers[self.spec.idempotency_header] = f"syncline-{self.run_id}-{action}-{label}"
        idempotent = endpoint.method != "POST" or bool(self.spec.idempotency_header)
        r = self.client.request(
            endpoint.method, path, idempotent=idempotent, json=body, headers=headers
        )
        if isinstance(r, str):
            raise _Stop(f"{endpoint.method} {path}: {r}")
        if r.status_code in _FATAL_STATUS:
            raise _Stop(f"{endpoint.method} {path}: {_message(r)} (check the credentials)")
        if r.status_code >= 400:
            return WriteFailure(action, ident, _message(r), r.status_code)
        return None
