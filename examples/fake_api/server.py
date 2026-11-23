"""A small fake HR/LMS-style JSON API, to try Syncline's REST target without a real one.

    python examples/fake_api/server.py --port 8765       # token: demo-token

Endpoints (Bearer token required), records in memory only:

    GET   /v1/people?page=1&page_size=100   → {"data": [...], "meta": {"page": 1, "total": 320}}
    POST  /v1/people                        → 201, or 409 if the id exists, 422 if invalid
    PATCH /v1/people/{externalId}           → 200, or 404 / 422
    PUT   /v1/people/{externalId}           → 200 (replaces the record)

JSON names are camelCase (externalId, firstName, hireDate...) to show `fields:` renames.
`Idempotency-Key` is honoured: a repeated key returns the first response without
applying the write again. Tests drive faults through `FakeApi` attributes (`faults`,
`refuse`, `drop_after_write`, `write_status`); stdlib only, no dependencies.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class FakeApi:
    token: str = "demo-token"
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Responses to return instead of handling the next requests: (status, headers).
    faults: list[tuple[int, dict[str, str]]] = field(default_factory=list)
    refuse: set[str] = field(default_factory=set)  # externalIds whose writes get a 422
    drop_after_write: int = 0  # apply the next N writes, then drop the connection (lost reply)
    write_status: int | None = None  # answer every write with this status, without applying it
    idempotency: dict[str, tuple[int, Any]] = field(default_factory=dict)
    log: list[tuple[str, str, Any]] = field(default_factory=list)  # (method, path, body)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def handle(
        self, method: str, raw_path: str, headers: Any, body: Any
    ) -> tuple[int, Any, dict[str, str]]:
        with self.lock:
            self.log.append((method, raw_path, body))
            if self.faults:
                status, extra = self.faults.pop(0)
                return status, {"error": f"injected {status}"}, extra
            if headers.get("Authorization") != f"Bearer {self.token}":
                return 401, {"error": "missing or wrong token"}, {}
            key = headers.get("Idempotency-Key")
            if key and method != "GET" and key in self.idempotency:
                status, payload = self.idempotency[key]
                return status, payload, {"Idempotent-Replayed": "true"}
            if self.write_status and method != "GET":
                return self.write_status, {"error": f"injected {self.write_status}"}, {}
            status, payload = self._route(method, urlparse(raw_path), body)
            if key and method != "GET":
                self.idempotency[key] = (status, payload)
            return status, payload, {}

    def _route(self, method: str, url: Any, body: Any) -> tuple[int, Any]:
        parts = [unquote(p) for p in url.path.strip("/").split("/")]
        if parts[:2] != ["v1", "people"] or len(parts) > 3:
            return 404, {"error": "not found"}
        if len(parts) == 2 and method == "GET":
            q = parse_qs(url.query)
            page, size = int(q.get("page", ["1"])[0]), int(q.get("page_size", ["100"])[0])
            items = sorted(self.records.values(), key=lambda r: r["externalId"])
            chunk = items[(page - 1) * size : page * size]
            return 200, {"data": chunk, "meta": {"page": page, "total": len(items)}}
        if len(parts) == 2 and method == "POST":
            problem = self._invalid(body, creating=True)
            if problem:
                return 422, {"error": problem}
            if body["externalId"] in self.records:
                return 409, {"error": f"{body['externalId']} already exists"}
            self.records[body["externalId"]] = dict(body)
            return 201, body
        if len(parts) == 3 and method in ("PATCH", "PUT"):
            ident = parts[2]
            if ident not in self.records:
                return 404, {"error": f"no person {ident}"}
            problem = self._invalid({**body, "externalId": ident}, creating=method == "PUT")
            if problem:
                return 422, {"error": problem}
            merged = dict(body) if method == "PUT" else {**self.records[ident], **body}
            self.records[ident] = {**merged, "externalId": ident}
            return 200, self.records[ident]
        return 405, {"error": "method not allowed"}

    def _invalid(self, body: Any, creating: bool) -> str | None:
        if not isinstance(body, dict):
            return "expected a JSON object"
        if body.get("externalId") in self.refuse:
            return f"{body['externalId']} is locked in the application"
        if creating and not body.get("externalId"):
            return "externalId is required"
        if "email" in body and not EMAIL.match(str(body["email"] or "")):
            return f"invalid email {body['email']!r}"
        return None


def make_server(api: FakeApi, port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload, extra = api.handle(self.command, self.path, self.headers, body)
            if self.command != "GET" and status < 400 and api.drop_after_write > 0:
                api.drop_after_write -= 1
                self.close_connection = True
                self.connection.close()  # the write happened; the reply is lost
                return
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = do_PUT = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.api = api  # type: ignore[attr-defined]  # for tests: inject faults, inspect records
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(FakeApi(), args.port)
    print(f"fake API on http://127.0.0.1:{args.port}  (token: demo-token)  Ctrl+C to stop")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
