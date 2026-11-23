"""S3 source, against moto's in-memory S3."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from moto import mock_aws

from syncline.engine import run
from syncline.io.fetch import _check_size, fetch, resolve_env
from syncline.io.read import SourceError
from syncline.spec import S3Source
from syncline.state import State

BUCKET = "acme-hr-exports"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "eu-west-3",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    with mock_aws():
        client = boto3.client("s3", region_name="eu-west-3")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-3"}
        )
        yield client


def src(**kw: Any) -> S3Source:
    return S3Source.model_validate(
        {"type": "s3", "bucket": BUCKET, "path": "hr/employes_*.csv", **kw}
    )


def test_downloads_the_last_matching_key(s3: Any, tmp_path: Path) -> None:
    s3.put_object(Bucket=BUCKET, Key="hr/employes_2026-11-02.csv", Body=b"id\n2\n")
    s3.put_object(Bucket=BUCKET, Key="hr/employes_2026-11-01.csv", Body=b"id\n1\n")  # newer upload
    s3.put_object(Bucket=BUCKET, Key="hr/old/employes_2026-12-31.csv", Body=b"x\n")  # sub-folder
    s3.put_object(Bucket=BUCKET, Key="hr/other.csv", Body=b"x\n")
    got = fetch(src(), tmp_path, tmp_path)
    assert got.path == tmp_path / "employes_2026-11-02.csv"
    assert got.path.read_bytes() == b"id\n2\n"
    assert got.origin == f"s3://{BUCKET}/hr/employes_2026-11-02.csv"


def test_key_at_the_bucket_root_and_many_pages(s3: Any, tmp_path: Path) -> None:
    for i in range(1005):  # more than one listing page (1000 keys)
        s3.put_object(Bucket=BUCKET, Key=f"export_{i:04d}.csv", Body=b"id\n")
    got = fetch(src(path="export_*.csv"), tmp_path, tmp_path)
    assert got.origin == f"s3://{BUCKET}/export_1004.csv"


def test_no_match(s3: Any, tmp_path: Path) -> None:
    with pytest.raises(
        SourceError, match=rf"no file matches employes_\*\.csv in s3://{BUCKET}/hr/"
    ):
        fetch(src(), tmp_path, tmp_path)


def test_missing_bucket_is_a_readable_error(s3: Any, tmp_path: Path) -> None:
    with pytest.raises(
        SourceError, match=r"S3 refused access to s3://nope-bucket/hr/: NoSuchBucket"
    ):
        fetch(src(bucket="nope-bucket"), tmp_path, tmp_path)


def test_unknown_profile(s3: Any, tmp_path: Path) -> None:
    with pytest.raises(SourceError, match=r"can't reach s3://.*profile"):
        fetch(src(profile="no-such-profile"), tmp_path, tmp_path)


def test_endpoint_from_the_environment(
    s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SourceError, match=r"S3_ENDPOINT is not set .endpoint_url"):
        fetch(src(endpoint_url="env:S3_ENDPOINT"), tmp_path, tmp_path)
    monkeypatch.setenv("S3_ENDPOINT", "https://s3.eu-west-3.amazonaws.com")
    s3.put_object(Bucket=BUCKET, Key="hr/employes_1.csv", Body=b"id\n")
    assert fetch(src(endpoint_url="env:S3_ENDPOINT"), tmp_path, tmp_path).path.is_file()


def test_resolve_env_leaves_plain_values() -> None:
    assert resolve_env("https://minio.local", "x") == "https://minio.local"


def test_incomplete_download_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "f.csv"
    f.write_bytes(b"12345")
    _check_size(f, 5, "s3://b/f.csv")
    with pytest.raises(SourceError, match=r"incomplete download of s3://b/f.csv: 5 of 9 bytes"):
        _check_size(f, 9, "s3://b/f.csv")


def test_a_local_file_override_wins_over_the_bucket(s3: Any, tmp_path: Path) -> None:
    f = tmp_path / "manual.csv"
    f.write_bytes(b"id\n1\n")
    assert fetch(src(), tmp_path, tmp_path, override=f).origin == str(f)


def test_a_full_run_from_s3(s3: Any, acme: Path, tmp_path: Path) -> None:
    s3.put_object(
        Bucket=BUCKET, Key="hr/employes_2026-11-01.csv",
        Body=(acme / "employes_2026-11-01.csv").read_bytes(),
    )  # fmt: skip
    flow = yaml.safe_load((acme / "acme_users.yaml").read_text(encoding="utf-8"))
    flow["source"] = {"type": "s3", "bucket": BUCKET, "path": "hr/employes_*.csv"}
    flow_file = acme / "acme_users_s3.yaml"
    flow_file.write_text(yaml.safe_dump(flow, allow_unicode=True), encoding="utf-8")
    res = run(flow_file, state=State(tmp_path / "s.db"))
    assert res.record.status == "ok"
    assert res.record.file == f"s3://{BUCKET}/hr/employes_2026-11-01.csv"
    assert res.record.created > 300
