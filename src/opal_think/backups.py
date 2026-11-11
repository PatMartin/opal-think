"""Service-managed backup archives (v1.7.1 kernel ``backup`` tag).

Archives are ``.tar.gz`` files under a ``backups/`` directory beside the configured
SQLite database (``db_path``'s parent, so per-test configs isolate them). Each archive
contains a single ``opal-think-config.json`` member — the same ``OpalConfigExport``
bundle shape served by ``GET /api/v1/backup/export`` — carrying portable settings,
personas, and profiles; never secrets or history.

Ported from opal-event's implementation (same kernel contract).
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from opal_think.errors import OpalHTTPException

BACKUP_MEMBER = "opal-think-config.json"
BACKUP_PATTERN = re.compile(r"^opal-think-\d{4}-\d{2}-\d{2}T\d{6}Z(-\d+)?\.tar\.gz$")
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024


def backup_dir(db_path: Path) -> Path:
    """Archive directory: ``<db dir>/backups`` — e.g. ``var/backups`` for the stock config."""
    return Path(db_path).parent / "backups"


def list_backups(directory: Path) -> list[dict]:
    """``OpalBackupInfo`` entries (filename, size_bytes, created_at), newest first."""
    if not directory.is_dir():
        return []
    items = []
    for path in directory.iterdir():
        if path.is_file() and BACKUP_PATTERN.match(path.name):
            stat = path.stat()
            items.append(
                {
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                }
            )
    return sorted(items, key=lambda b: (b["created_at"], b["filename"]), reverse=True)


def create_backup(directory: Path, bundle: dict) -> str:
    """Write a new timestamped archive holding `bundle`; returns the filename."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("opal-think-%Y-%m-%dT%H%M%SZ")
    filename, n = f"{stem}.tar.gz", 1
    while (directory / filename).exists():
        filename, n = f"{stem}-{n}.tar.gz", n + 1
    payload = json.dumps(bundle, indent=2).encode()
    tmp = directory / f".{filename}.tmp"
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            info = tarfile.TarInfo(BACKUP_MEMBER)
            info.size = len(payload)
            info.mtime = int(datetime.now(UTC).timestamp())
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(payload))
        tmp.replace(directory / filename)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return filename


def backup_path(directory: Path, filename: str) -> Path:
    """Resolve a caller-supplied filename to an existing archive, or 404.

    The kernel ``BackupFilename`` pattern (``^[^/\\\\]+$``) is enforced; anything else
    is a 404 rather than a traversal opportunity."""
    if not filename or "/" in filename or "\\" in filename or not BACKUP_PATTERN.match(filename):
        raise OpalHTTPException(404, "not_found", f"backup '{filename}' not found")
    path = directory / filename
    if not path.is_file():
        raise OpalHTTPException(404, "not_found", f"backup '{filename}' not found")
    return path


def read_bundle(directory: Path, filename: str) -> dict:
    """Extract the config-bundle JSON member from an archive (bounded size)."""
    path = backup_path(directory, filename)
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise OpalHTTPException(409, "conflict", f"backup '{filename}' exceeds maximum archive size")
    try:
        with tarfile.open(path, "r:gz") as tar:
            member = tar.extractfile(tar.getmember(BACKUP_MEMBER))
            if member is None:
                raise ValueError("missing config member")
            return json.loads(member.read().decode("utf-8"))
    except (tarfile.TarError, KeyError, OSError, ValueError, UnicodeDecodeError) as exc:
        raise OpalHTTPException(409, "conflict", f"backup '{filename}' is not a valid opal-think archive: {exc}") from exc
