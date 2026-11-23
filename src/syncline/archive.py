"""Raw-file archive: every input file is kept, unchanged, before it is processed.

Files are stored by content (`<root>/<flow>/<sha256><ext>`), so the same file is kept
once however many runs read it, and nothing is ever overwritten. Stored files are
read-only and checked against their hash when they are used again: `syncline replay`
re-runs a past run on exactly the bytes it read.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path


def default_archive() -> Path:
    """SYNCLINE_ARCHIVE, or ~/.syncline/archive. Read at call time, not import time."""
    return Path(os.environ.get("SYNCLINE_ARCHIVE", Path.home() / ".syncline" / "archive"))


class ArchiveError(Exception):
    """An archived file is missing or no longer matches its hash."""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Archive:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_archive()

    def path_for(self, flow: str, sha256: str, suffix: str) -> Path:
        return self.root / flow / f"{sha256}{suffix.lower()}"

    def store(self, flow: str, source: Path, sha256: str) -> Path:
        """Keep a copy of `source` (whose content hash is `sha256`). Idempotent."""
        target = self.path_for(flow, sha256, source.suffix)
        if target.exists():
            self.verify(target, sha256)
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ArchiveError(f"{source} changed while it was being read; run again")
        # Write to a temp file in the same folder, then rename: never a half-written archive.
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, stat.S_IRUSR | stat.S_IRGRP)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def verify(path: Path, sha256: str) -> Path:
        if not path.is_file():
            raise ArchiveError(f"archived file is missing: {path}")
        actual = sha256_of(path)
        if actual != sha256:
            raise ArchiveError(
                f"archived file {path.name} doesn't match its hash (expected {sha256[:12]}, "
                f"found {actual[:12]}): it was modified, refusing to use it"
            )
        return path
