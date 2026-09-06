from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile

from fastapi.testclient import TestClient
import pytest

import service
from organize import analyze, chinese_number, safe_name
from backups import validate_archive


@pytest.mark.parametrize(("text", "expected"), [("十二", 12), ("一百零二", 102), ("两千零三", 2003), ("一万零一", 10001), ("〇一二", 12), ("001", 1)])
def test_chinese_numbers(text: str, expected: int) -> None:
    assert chinese_number(text) == expected


def test_chapter_order_and_warnings() -> None:
    result = analyze(["第十二集.mp3", "第2集.mp3", "第十一集.mp3", "第十一集下.mp3"])
    assert result["order"] == [1, 2, 3, 0]
    assert result["duplicateNumbers"] == [11]
    assert result["missing"] == list(range(3, 11))


def test_volume_order_does_not_confuse_volume_and_chapter_numbers() -> None:
    result = analyze(["第二部 第1集.mp3", "第一部 第十二集.mp3", "第一部 第2集.mp3"])
    assert result["order"] == [2, 1, 0]
    assert result["duplicateNumbers"] == []


def test_safe_name_rejects_empty_and_limits_path_components() -> None:
    assert "/" not in safe_name("../书名/章节")
    with pytest.raises(ValueError):
        safe_name("..")


class FakeBucket:
    bucket_name = "test-books"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.parts: dict[str, dict[int, bytes]] = {}

    def init_multipart_upload(self, key: str) -> SimpleNamespace:
        self.parts[key] = {}
        return SimpleNamespace(upload_id=key)

    def upload_part(self, key: str, upload_id: str, number: int, content: bytes) -> None:
        self.parts[key][number] = content

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[object]) -> None:
        self.objects[key] = b"".join(self.parts[key][number] for number in sorted(self.parts[key]))

    def head_object(self, key: str) -> SimpleNamespace:
        return SimpleNamespace(content_length=len(self.objects[key]))

    def put_object(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    def copy_object(self, source_bucket: str, source: str, target: str) -> None:
        self.objects[target] = self.objects[source]

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeBucket, list[str]]:
    bucket = FakeBucket()
    calls: list[str] = []
    monkeypatch.setattr(service, "settings", replace(service.settings, data=tmp_path))
    monkeypatch.setattr(service, "connect", lambda public=False: bucket)
    monkeypatch.setattr(service, "finish_catalog", lambda job, user: job.update(bookId="fixture-book"))
    monkeypatch.setattr(service.oss2, "PartIterator", lambda bucket, key, upload_id: [SimpleNamespace(part_number=number, size=len(data), etag=str(number)) for number, data in sorted(bucket.parts[key].items())])

    def api(method: str, path: str, token: str, **kwargs: object) -> SimpleNamespace:
        calls.append(path)
        if path == "/api/me":
            return SimpleNamespace(json=lambda: {"id": "other" if token == "Bearer other" else "owner", "type": "user" if token == "Bearer reader" else "root"})
        return SimpleNamespace(json=lambda: {"libraries": [{"id": "books", "mediaType": "book", "folders": [{"fullPath": "/audiobooks"}]}]})
    monkeypatch.setattr(service, "abs_request", api)
    client = TestClient(service.app, headers={"Authorization": "Bearer owner"})
    return client, bucket, calls


def manifest() -> dict[str, object]:
    return {"title": "测试书", "library_id": "books", "author": "作者", "files": [{"name": "第1集.wav", "size": 8, "fingerprint": "a" * 64}]}


def test_auth_required_and_readers_cannot_admin(setup: tuple) -> None:
    client, _, _ = setup
    assert client.get("/personal/api/libraries", headers={"Authorization": ""}).status_code == 401
    assert client.get("/personal/api/libraries", headers={"Authorization": "Bearer reader"}).status_code == 403


def test_import_is_idempotent_and_owner_isolated(setup: tuple) -> None:
    client, _, _ = setup
    first = client.post("/personal/api/imports", json=manifest()).json()
    assert client.post("/personal/api/imports", json=manifest()).json()["id"] == first["id"]
    assert client.get(f"/personal/api/imports/{first['id']}", headers={"Authorization": "Bearer other"}).status_code == 404


def test_rejects_path_traversal_and_wrong_library(setup: tuple) -> None:
    client, _, _ = setup
    spec = manifest(); spec["files"][0]["name"] = "../secret.mp3"
    assert client.post("/personal/api/imports", json=spec).status_code == 422
    spec = manifest(); spec["library_id"] = "other"
    assert client.post("/personal/api/imports", json=spec).status_code == 403


def test_upload_restarts_without_losing_parts_and_publishes_metadata(setup: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    client, bucket, calls = setup
    monkeypatch.setattr(service, "PART_SIZE", 4)
    job = client.post("/personal/api/imports", json=manifest()).json()
    base = f"/personal/api/imports/{job['id']}"
    file_base = f"{base}/files/{job['files'][0]['id']}"
    assert client.post(base + "/complete").status_code == 409
    assert client.put(file_base + "/parts/1", content=b"abcde").status_code == 413
    assert client.put(file_base + "/parts/1", content=b"abcd").status_code == 200
    assert client.get(base).json()["files"][0]["parts"] == [{"number": 1, "size": 4}]
    assert client.post(file_base + "/complete").status_code == 409
    assert client.put(file_base + "/parts/2", content=b"efgh").status_code == 200
    assert client.post(file_base + "/complete").status_code == 200
    result = client.post(base + "/complete")
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "complete"
    assert bucket.objects[job["destination"] + "/0001-第1集.wav"] == b"abcdefgh"
    assert json.loads(bucket.objects[job["destination"] + "/metadata.json"])["authors"] == ["作者"]
    assert calls.count("/api/libraries/books/scan") == 1
    assert client.post(base + "/complete").status_code == 200
    assert calls.count("/api/libraries/books/scan") == 1


def test_completed_file_commit_can_be_retried_after_response_loss(setup: tuple) -> None:
    client, bucket, _ = setup
    job = client.post("/personal/api/imports", json=manifest()).json()
    file = job["files"][0]
    bucket.objects[file["key"]] = b"12345678"
    assert client.post(f"/personal/api/imports/{job['id']}/files/{file['id']}/complete").status_code == 200


def test_restore_checks_real_sqlite_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "sample.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE Progress (position REAL)")
        db.execute("INSERT INTO Progress VALUES (123.5)")
    archive = tmp_path / "sample.audiobookshelf"
    with zipfile.ZipFile(archive, "w") as output:
        output.write(database, "absdatabase.sqlite")
        output.writestr("details", "test")
    assert validate_archive(archive)["integrity"] == "ok"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("absdatabase.sqlite", b"not a database")
        output.writestr("details", "test")
    with pytest.raises(sqlite3.DatabaseError):
        validate_archive(archive)


def test_missing_chapters_are_checked_inside_each_volume() -> None:
    assert analyze(["第一部 第1集.mp3", "第一部 第2集.mp3", "第二部 第10集.mp3", "第二部 第11集.mp3"])["missing"] == []


def test_deleted_book_can_be_imported_again(setup: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = setup
    first = client.post("/personal/api/imports", json=manifest()).json()
    first.update(state="complete", bookId="deleted-book")
    service.save(first)
    original = service.abs_request
    def api(method: str, path: str, token: str, **kwargs: object) -> object:
        if path == "/api/items/deleted-book":
            raise service.HTTPException(404)
        return original(method, path, token, **kwargs)
    monkeypatch.setattr(service, "abs_request", api)
    second = client.post("/personal/api/imports", json=manifest()).json()
    assert second["id"] != first["id"]
