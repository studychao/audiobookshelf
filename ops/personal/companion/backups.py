"""Consistent ABS exports, verified private OSS copies, and safe restore checks."""
from __future__ import annotations

import argparse
import fcntl
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

import httpx
import oss2

from service import abs_request, connect, settings

PREFIX = "audiobookshelf/backups/"


def validate_archive(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        if "absdatabase.sqlite" not in archive.namelist() or "details" not in archive.namelist():
            raise ValueError("Backup is missing the ABS database or version details")
        if archive.testzip() is not None:
            raise ValueError("Backup archive CRC verification failed")
        with tempfile.TemporaryDirectory(prefix="abs-restore-check-") as directory:
            target = Path(directory) / "absdatabase.sqlite"
            with archive.open("absdatabase.sqlite") as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as database:
                checks = database.execute("PRAGMA integrity_check").fetchall()
                if checks != [("ok",)]:
                    raise ValueError("Backup SQLite integrity check failed")
                tables = [row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {"integrity": "ok", "tables": len(tables), "entries": len(archive.infolist())}


def create_backup(token: str) -> dict[str, object]:
    settings.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (settings.data / "backup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _create_backup(token)


def _create_backup(token: str) -> dict[str, object]:
    result = abs_request("POST", "/api/backups", token).json()
    latest = max(result["backups"], key=lambda backup: backup["createdAt"])
    with tempfile.TemporaryDirectory(prefix="abs-backup-") as directory:
        path = Path(directory) / "backup.audiobookshelf"
        digest = hashlib.sha256()
        with httpx.stream("GET", f"{settings.abs_url}/api/backups/{latest['id']}/download", headers={"Authorization": token}, timeout=300) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    output.write(chunk)
        verification = validate_archive(path)
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        key = f"{PREFIX}{now}.audiobookshelf"
        sha256 = digest.hexdigest()
        bucket = connect()
        bucket.put_object_from_file(key, str(path), headers={"x-oss-server-side-encryption": "AES256", "x-oss-meta-sha256": sha256})
        downloaded_hash = hashlib.sha256()
        with bucket.get_object(key) as stored:
            for chunk in iter(lambda: stored.read(1024 * 1024), b""):
                downloaded_hash.update(chunk)
        if downloaded_hash.hexdigest() != sha256:
            raise ValueError("OSS backup verification failed; existing backups retained")
        record = {"key": key, "size": path.stat().st_size, "sha256": sha256, "createdAt": now, **verification}
        bucket.put_object(key + ".json", json.dumps(record).encode(), headers={"x-oss-server-side-encryption": "AES256"})
        # Delete only older archives created by this feature, after the new copy verifies.
        archives = sorted(item.key for item in oss2.ObjectIterator(bucket, prefix=PREFIX) if item.key.endswith(".audiobookshelf"))
        for old in archives[:-14]:
            bucket.delete_object(old)
            bucket.delete_object(old + ".json")
        settings.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        (settings.data / "last-backup.json").write_text(json.dumps(record))
        return record


def download_and_check(key: str, output: Path) -> dict[str, object]:
    if not key.startswith(PREFIX) or not key.endswith(".audiobookshelf") or "/" in key[len(PREFIX):]:
        raise ValueError("Select an archive from this application's backup prefix")
    if output.exists():
        raise FileExistsError(f"Will not overwrite an existing file: {output}")
    bucket = connect()
    record = json.loads(bucket.get_object(key + ".json").read())
    bucket.get_object_to_file(key, str(output))
    with output.open("rb") as downloaded:
        digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
    if digest != record["sha256"]:
        output.unlink()
        raise ValueError("Downloaded backup hash does not match its manifest")
    return validate_archive(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-restore", help="OSS backup key to download and verify without modifying the server")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.check_restore:
        if not args.output:
            parser.error("--output is required for restore verification")
        print(json.dumps(download_and_check(args.check_restore, args.output)))
        return
    credentials = json.loads(Path("/opt/audiobookshelf/secrets/admin.json").read_text())
    response = abs_request("POST", "/login", "", json=credentials).json()["user"]
    token = response.get("accessToken") or response["token"]
    print(json.dumps(create_backup(f"Bearer {token}")))


if __name__ == "__main__":
    main()
