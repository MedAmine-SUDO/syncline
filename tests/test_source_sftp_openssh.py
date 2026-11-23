"""SFTP source against a real OpenSSH server (the atmoz/sftp container).

Skipped unless SYNCLINE_TEST_SFTP_PORT is set; CI starts the container. Locally:

    mkdir -p /tmp/sftp-out && cp examples/acme/employes_2026-11-01.csv /tmp/sftp-out/
    docker run -d --name syncline-sftp -p 2222:22 \\
        -v /tmp/sftp-out:/home/acme/outgoing:ro atmoz/sftp acme:acme-test-pw:1001
    ssh-keyscan -p 2222 127.0.0.1 > /tmp/sftp_known_hosts
    SYNCLINE_TEST_SFTP_PORT=2222 SYNCLINE_TEST_SFTP_KNOWN_HOSTS=/tmp/sftp_known_hosts \\
        SYNCLINE_TEST_SFTP_PASSWORD=acme-test-pw uv run pytest tests/test_source_sftp_openssh.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from syncline.io.fetch import fetch
from syncline.io.read import read_file
from syncline.spec import SftpSource

PORT = os.environ.get("SYNCLINE_TEST_SFTP_PORT")
pytestmark = pytest.mark.skipif(not PORT, reason="set SYNCLINE_TEST_SFTP_PORT (see docstring)")


def test_reads_the_acme_export_from_openssh(tmp_path: Path) -> None:
    src = SftpSource(
        type="sftp", host="127.0.0.1", port=int(PORT or 22), username="acme",
        password="env:SYNCLINE_TEST_SFTP_PASSWORD",
        known_hosts=os.environ["SYNCLINE_TEST_SFTP_KNOWN_HOSTS"],
        path="outgoing/employes_*.csv", min_age_seconds=0,
    )  # fmt: skip
    got = fetch(src, tmp_path, tmp_path)
    assert got.origin == f"sftp://127.0.0.1:{PORT}/~/outgoing/employes_2026-11-01.csv"
    assert read_file(got.path, src).df.height == 323
