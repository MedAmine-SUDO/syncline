"""Getting the input file onto local disk, wherever it lives: a folder, S3 or SFTP.

Every source works the same way: list the candidates, keep those whose name matches the
pattern, take the **last by name** (dated names sort correctly; modification times don't
survive copies), download it into the run's temporary folder, check the download is
complete. From there the run is identical for every source: read, archive, map...

Credentials never come from the flow file. S3 uses the standard AWS chain (environment,
profile, instance role); SFTP passwords and key passphrases are `env:VARIABLE` references.
The S3 and SFTP clients are optional extras (`syncline[s3]`, `syncline[sftp]`), imported
only when such a source is used.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from syncline.io.read import SourceError, find_file
from syncline.spec import FileSource, S3Source, SftpSource, Source

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


@dataclass(frozen=True)
class Fetched:
    path: Path  # local file to read
    origin: str  # where it came from, for the run history: a path, s3://... or sftp://...


def fetch(source: Source, base_dir: Path, workdir: Path, override: Path | None = None) -> Fetched:
    """The run's input file, on local disk. `override` (a local file) wins over the source."""
    if override is not None:
        path = find_file(FileSource(path=override.name), base_dir, override)
        return Fetched(path=path, origin=str(path))
    if isinstance(source, FileSource):
        path = find_file(source, base_dir)
        return Fetched(path=path, origin=str(path))
    if isinstance(source, S3Source):
        return _fetch_s3(source, workdir)
    return _fetch_sftp(source, base_dir, workdir)


def resolve_env(value: str, what: str) -> str:
    """`env:NAME` → the value of $NAME; anything else unchanged."""
    if not value.startswith("env:"):
        return value
    name = value.removeprefix("env:")
    resolved = os.environ.get(name)
    if not resolved:
        raise SourceError(f"environment variable {name} is not set ({what})")
    return resolved


def _split(path: str) -> tuple[str, str]:
    folder, _, pattern = path.rpartition("/")
    return folder, pattern


def _pick(names: list[str], pattern: str, where: str) -> str:
    matches = sorted(n for n in names if fnmatch.fnmatchcase(n, pattern))
    if not matches:
        raise SourceError(f"no file matches {pattern} in {where}")
    return matches[-1]


def _check_size(path: Path, expected: int, origin: str) -> None:
    actual = path.stat().st_size
    if actual != expected:
        raise SourceError(f"incomplete download of {origin}: {actual} of {expected} bytes")


# --- S3 ---------------------------------------------------------------------------------


def _s3_client(source: S3Source) -> S3Client:
    try:
        import boto3
    except ImportError:  # pragma: no cover: the s3 extra is installed in tests
        raise SourceError("S3 sources need the s3 extra: pip install 'syncline[s3]'") from None
    endpoint = resolve_env(source.endpoint_url, "endpoint_url") if source.endpoint_url else None
    session = boto3.session.Session(profile_name=source.profile, region_name=source.region)
    return session.client("s3", endpoint_url=endpoint)


def _fetch_s3(source: S3Source, workdir: Path) -> Fetched:
    from botocore.exceptions import BotoCoreError, ClientError

    folder, pattern = _split(source.path)
    prefix = f"{folder}/" if folder else ""
    where = f"s3://{source.bucket}/{prefix}"
    try:
        client = _s3_client(source)
        sizes: dict[str, int] = {}
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=source.bucket, Prefix=prefix, Delimiter="/"
        ):
            for obj in page.get("Contents", []):
                sizes[obj["Key"].removeprefix(prefix)] = obj["Size"]
        name = _pick(list(sizes), pattern, where)
        key = prefix + name
        dest = workdir / name
        client.download_file(source.bucket, key, str(dest))
    except ClientError as e:
        error = e.response.get("Error", {})
        raise SourceError(
            f"S3 refused access to {where}: {error.get('Code')} {error.get('Message', '')}".rstrip()
        ) from None
    except BotoCoreError as e:  # no credentials, unreachable endpoint, ...
        raise SourceError(f"can't reach {where}: {e}") from None
    origin = f"s3://{source.bucket}/{key}"
    _check_size(dest, sizes[name], origin)
    return Fetched(path=dest, origin=origin)


# --- SFTP -------------------------------------------------------------------------------


def _sftp_url(server: str, path: str) -> str:
    # sftp://host/abs/path for absolute paths, sftp://host/~/path for paths from the login folder
    return f"sftp://{server}{path}" if path.startswith("/") else f"sftp://{server}/~/{path}"


def _fetch_sftp(source: SftpSource, base_dir: Path, workdir: Path) -> Fetched:
    try:
        import paramiko
    except ImportError:  # pragma: no cover: the sftp extra is installed in tests
        raise SourceError(
            "SFTP sources need the sftp extra: pip install 'syncline[sftp]'"
        ) from None

    folder, pattern = _split(source.path)
    server = source.host if source.port == 22 else f"{source.host}:{source.port}"
    where = _sftp_url(server, folder or ".")
    known_hosts = Path(source.known_hosts or "~/.ssh/known_hosts").expanduser()
    if not known_hosts.is_absolute():
        known_hosts = base_dir / known_hosts
    key_file = None
    if source.key_file:
        key_file = Path(source.key_file).expanduser()
        if not key_file.is_absolute():
            key_file = base_dir / key_file
    password = resolve_env(source.password, "SFTP password") if source.password else None
    passphrase = (
        resolve_env(source.key_passphrase, "SFTP key passphrase") if source.key_passphrase else None
    )
    connect: dict[str, Any] = dict(
        hostname=source.host,
        port=source.port,
        username=resolve_env(source.username, "SFTP username"),
        password=password,
        key_filename=str(key_file) if key_file else None,
        passphrase=passphrase,
        timeout=source.timeout_seconds,
        banner_timeout=source.timeout_seconds,
        auth_timeout=source.timeout_seconds,
        # No password and no key: fall back to the SSH agent and ~/.ssh keys, like `sftp` does.
        allow_agent=password is None and key_file is None,
        look_for_keys=password is None and key_file is None,
    )

    client = paramiko.SSHClient()
    try:
        if known_hosts.is_file():
            client.load_host_keys(str(known_hosts))
        # Never trust an unknown server: a wrong host key is how files get stolen or swapped.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(**connect)
        with client.open_sftp() as sftp:
            entries = {
                e.filename: e
                for e in sftp.listdir_attr(folder or ".")
                if e.st_mode is not None and stat.S_ISREG(e.st_mode)
            }
            names = list(entries)
            fresh = []
            if source.min_age_seconds:
                cutoff = time.time() - source.min_age_seconds
                fresh = [n for n in names if (entries[n].st_mtime or 0) > cutoff]
                names = [n for n in names if n not in fresh]
            try:
                name = _pick(names, pattern, where)
            except SourceError:
                if any(fnmatch.fnmatchcase(n, pattern) for n in fresh):
                    raise SourceError(
                        f"the only file(s) matching {pattern} in {where} were modified less than "
                        f"{source.min_age_seconds}s ago (maybe still uploading); try again later"
                    ) from None
                raise
            remote = posixpath.join(folder, name) if folder else name
            dest = workdir / name
            sftp.get(remote, str(dest))
    except paramiko.BadHostKeyException:
        raise SourceError(
            f"the host key of {server} doesn't match {known_hosts}: refusing to connect "
            "(the server changed, or someone is in between); check with the server's owner"
        ) from None
    except paramiko.AuthenticationException:
        raise SourceError(f"SFTP login refused by {server} for {connect['username']}") from None
    except paramiko.SSHException as e:
        if "not found in known_hosts" in str(e):
            host = source.host if source.port == 22 else f"[{source.host}]:{source.port}"
            raise SourceError(
                f"{host} is not in {known_hosts}: refusing an unknown server. After checking "
                f"its fingerprint with the server's owner: "
                f"ssh-keyscan -p {source.port} {source.host} >> {known_hosts}"
            ) from None
        raise SourceError(f"SFTP error with {server}: {e}") from None
    except OSError as e:  # unreachable host, timeout, missing folder or key file
        raise SourceError(f"SFTP: can't read {where}: {e.strerror or e}") from None
    finally:
        client.close()
    origin = _sftp_url(server, remote)
    _check_size(dest, entries[name].st_size or 0, origin)
    return Fetched(path=dest, origin=origin)
