"""SFTP source, against a real SSH/SFTP server running in the test process (paramiko).

The server speaks the real protocol over a TCP socket: host keys, password and key logins,
directory listings and downloads all go over the wire, like with any SFTP server.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paramiko
import pytest
import yaml

from syncline.engine import run
from syncline.io.fetch import fetch
from syncline.io.read import SourceError
from syncline.spec import SftpSource
from syncline.state import State

USER, PASSWORD = "acme", "s3cret-for-tests"
OLD = time.time() - 3600  # an hour ago: well past min_age_seconds


class _Server(paramiko.ServerInterface):
    def __init__(self, client_key: paramiko.PKey) -> None:
        self.client_key = client_key

    def check_auth_password(self, username: str, password: str) -> int:
        ok = (username, password) == (USER, PASSWORD)
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        ok = username == USER and key == self.client_key
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password,publickey"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return paramiko.OPEN_SUCCEEDED


class _Handle(paramiko.SFTPHandle):
    def stat(self) -> paramiko.SFTPAttributes | int:
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))  # type: ignore[attr-defined]


class _Files(paramiko.SFTPServerInterface):
    """Serves a local folder; paths starting with / are relative to it, others to home/."""

    root: Path

    def _local(self, path: str) -> Path:
        return self.root / path.lstrip("/") if path.startswith("/") else self.root / "home" / path

    def list_folder(self, path: str) -> list[paramiko.SFTPAttributes] | int:
        folder = self._local(path)
        if not folder.is_dir():
            return paramiko.SFTP_NO_SUCH_FILE
        return [
            paramiko.SFTPAttributes.from_stat(p.stat(), p.name) for p in sorted(folder.iterdir())
        ]

    def stat(self, path: str) -> paramiko.SFTPAttributes | int:
        p = self._local(path)
        return (
            paramiko.SFTPAttributes.from_stat(p.stat())
            if p.exists()
            else paramiko.SFTP_NO_SUCH_FILE
        )

    lstat = stat

    def open(
        self, path: str, flags: int, attr: paramiko.SFTPAttributes
    ) -> paramiko.SFTPHandle | int:
        p = self._local(path)
        if not p.is_file():
            return paramiko.SFTP_NO_SUCH_FILE
        handle = _Handle(flags)
        handle.readfile = p.open("rb")  # type: ignore[attr-defined]
        return handle


@dataclass
class SftpServer:
    port: int
    root: Path  # what the server serves
    host_key: paramiko.PKey
    client_key_file: Path
    known_hosts: Path

    def put(self, remote: str, content: bytes, mtime: float = OLD) -> Path:
        local = (
            self.root / remote.lstrip("/")
            if remote.startswith("/")
            else self.root / "home" / remote
        )
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        os.utime(local, (mtime, mtime))
        return local

    def source(self, **kw: Any) -> SftpSource:
        base: dict[str, Any] = dict(
            type="sftp", host="127.0.0.1", port=self.port, username=USER,
            password="env:TEST_SFTP_PASSWORD", known_hosts=str(self.known_hosts),
            path="/outgoing/employes_*.csv",
        )  # fmt: skip
        base.update(kw)
        return SftpSource.model_validate(base)


@pytest.fixture
def sftp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SftpServer]:
    monkeypatch.setenv("TEST_SFTP_PASSWORD", PASSWORD)
    host_key = paramiko.Ed25519Key.from_private_key_file(_new_key(tmp_path / "host_key"))
    client_key_file = Path(_new_key(tmp_path / "client_key"))
    client_key = paramiko.Ed25519Key.from_private_key_file(str(client_key_file))
    root = tmp_path / "server"
    (root / "home").mkdir(parents=True)
    files = type("Files", (_Files,), {"root": root})

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    transports: list[paramiko.Transport] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, files)
            transports.append(t)
            try:
                t.start_server(server=_Server(client_key))
            except (paramiko.SSHException, EOFError, OSError):
                continue

    threading.Thread(target=serve, daemon=True).start()
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n")
    yield SftpServer(port, root, host_key, client_key_file, known_hosts)
    listener.close()
    for t in transports:
        t.close()


def _new_key(path: Path) -> str:
    # paramiko can't generate Ed25519 keys; ssh-keygen can, and it's everywhere OpenSSH is.
    if shutil.which("ssh-keygen") is None:  # pragma: no cover
        pytest.skip("ssh-keygen not available")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return str(path)


def test_downloads_the_last_matching_file_by_name(sftp: SftpServer, tmp_path: Path) -> None:
    sftp.put("/outgoing/employes_2026-11-01.csv", b"id\n1\n")
    sftp.put("/outgoing/employes_2026-11-02.csv", b"id\n2\n", mtime=OLD - 600)  # older mtime
    sftp.put("/outgoing/other.csv", b"x\n")
    work = tmp_path / "work"
    work.mkdir()
    got = fetch(sftp.source(), tmp_path, work)
    assert got.path == work / "employes_2026-11-02.csv"
    assert got.path.read_bytes() == b"id\n2\n"
    assert got.origin == f"sftp://127.0.0.1:{sftp.port}/outgoing/employes_2026-11-02.csv"


def test_relative_path_is_from_the_login_folder_and_key_login_works(
    sftp: SftpServer, tmp_path: Path
) -> None:
    sftp.put("drop/export.csv", b"id\n1\n")
    src = sftp.source(path="drop/*.csv", password=None, key_file=str(sftp.client_key_file))
    got = fetch(src, tmp_path, tmp_path)
    assert got.origin == f"sftp://127.0.0.1:{sftp.port}/~/drop/export.csv"


def test_a_file_still_being_uploaded_is_ignored(sftp: SftpServer, tmp_path: Path) -> None:
    sftp.put("/outgoing/employes_2026-11-01.csv", b"id\n1\n")
    sftp.put("/outgoing/employes_2026-11-02.csv", b"id\n", mtime=time.time())  # just written
    assert fetch(sftp.source(), tmp_path, tmp_path).path.name == "employes_2026-11-01.csv"
    assert fetch(sftp.source(min_age_seconds=0), tmp_path, tmp_path).path.name.endswith("02.csv")


def test_only_fresh_files_says_so(sftp: SftpServer, tmp_path: Path) -> None:
    sftp.put("/outgoing/employes_2026-11-02.csv", b"id\n", mtime=time.time())
    with pytest.raises(SourceError, match=r"less than 60s ago .maybe still uploading"):
        fetch(sftp.source(), tmp_path, tmp_path)


def test_no_match_and_missing_folder(sftp: SftpServer, tmp_path: Path) -> None:
    sftp.put("/outgoing/other.csv", b"x\n")
    with pytest.raises(SourceError, match=r"no file matches employes_\*\.csv in sftp://"):
        fetch(sftp.source(), tmp_path, tmp_path)
    with pytest.raises(SourceError, match=r"SFTP: can't read"):
        fetch(sftp.source(path="/nope/*.csv"), tmp_path, tmp_path)


def test_unknown_server_is_refused_with_how_to_trust_it(sftp: SftpServer, tmp_path: Path) -> None:
    empty = tmp_path / "empty_known_hosts"
    with pytest.raises(SourceError, match=r"not in .*refusing an unknown server.*ssh-keyscan -p"):
        fetch(sftp.source(known_hosts=str(empty)), tmp_path, tmp_path)


def test_changed_host_key_is_refused(sftp: SftpServer, tmp_path: Path) -> None:
    other = paramiko.Ed25519Key.from_private_key_file(_new_key(tmp_path / "other"))
    wrong = tmp_path / "wrong_known_hosts"
    wrong.write_text(f"[127.0.0.1]:{sftp.port} {other.get_name()} {other.get_base64()}\n")
    with pytest.raises(SourceError, match=r"doesn't match .*refusing to connect"):
        fetch(sftp.source(known_hosts=str(wrong)), tmp_path, tmp_path)


def test_wrong_password_and_missing_variable(
    sftp: SftpServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_SFTP_PASSWORD", "wrong")
    with pytest.raises(SourceError, match=r"login refused by 127.0.0.1:.* for acme"):
        fetch(sftp.source(), tmp_path, tmp_path)
    monkeypatch.delenv("TEST_SFTP_PASSWORD")
    with pytest.raises(SourceError, match=r"TEST_SFTP_PASSWORD is not set .SFTP password"):
        fetch(sftp.source(), tmp_path, tmp_path)


def test_unreachable_server(tmp_path: Path) -> None:
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]  # closed right after: nothing listens there
    src = SftpSource(
        type="sftp", host="127.0.0.1", port=port, username="u", path="/x/*.csv",
        known_hosts=str(tmp_path / "kh"), timeout_seconds=2,
    )  # fmt: skip
    with pytest.raises(SourceError, match=r"SFTP: can't read"):
        fetch(src, tmp_path, tmp_path)


def test_a_full_run_from_sftp_records_where_the_file_came_from(
    sftp: SftpServer, acme: Path, tmp_path: Path
) -> None:
    sftp.put("/outgoing/employes_2026-11-01.csv", (acme / "employes_2026-11-01.csv").read_bytes())
    flow = yaml.safe_load((acme / "acme_users.yaml").read_text(encoding="utf-8"))
    flow["source"] = sftp.source().model_dump(exclude_none=True)
    flow_file = acme / "acme_users_sftp.yaml"
    flow_file.write_text(yaml.safe_dump(flow, allow_unicode=True), encoding="utf-8")

    from syncline.archive import Archive

    state = State(tmp_path / "s.db")
    res = run(flow_file, state=state, archive=Archive(tmp_path / "archive"))
    assert res.record.status == "ok"
    assert res.record.created > 300
    assert res.record.file == f"sftp://127.0.0.1:{sftp.port}/outgoing/employes_2026-11-01.csv"
    assert (
        res.record.archived and Path(res.record.archived).is_file()
    )  # kept after the temp copy is gone
    again = run(flow_file, state=state)
    assert again.record.status == "skipped"  # same bytes, wherever they come from
