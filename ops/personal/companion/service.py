"""Personal import companion. ABS owns authentication, books and playback data."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import threading
import time
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import httpx
import oss2
from oss2.models import PartInfo
from pydantic import BaseModel, Field, field_validator

from organize import AUDIO, IMAGES, analyze, safe_name

log = logging.getLogger("audiobookshelf.personal")
PART_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    data: Path = Path(os.environ.get("PERSONAL_DATA", "/opt/audiobookshelf/personal-data"))
    credentials: Path = Path(os.environ.get("OSS_CREDENTIALS", "/opt/audiobookshelf/secrets/oss-passwd"))
    abs_url: str = os.environ.get("ABS_URL", "http://127.0.0.1:13378")
    region: str = os.environ.get("OSS_REGION", "us-west-1")
    origin: str = os.environ.get("PUBLIC_ORIGIN", "https://audiobook.47-77-238-23.sslip.io")


settings = Settings()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=[settings.origin, "capacitor://localhost", "http://localhost"], allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"], allow_credentials=False)
locks: dict[str, threading.Lock] = {}
locks_guard = threading.Lock()


@app.exception_handler(oss2.exceptions.OssError)
async def storage_error(request: Request, error: oss2.exceptions.OssError) -> JSONResponse:
    log.error("Storage request failed: %s", type(error).__name__)
    return JSONResponse(status_code=502, content={"detail": "存储暂时不可用，已完成的分片会保留，请稍后重试"})


@app.exception_handler(httpx.HTTPError)
async def upstream_error(request: Request, error: httpx.HTTPError) -> JSONResponse:
    log.error("ABS request failed: %s", type(error).__name__)
    return JSONResponse(status_code=502, content={"detail": "书库暂时不可连接，请稍后重试"})


@app.middleware("http")
async def response_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store" if "/api/" in request.url.path else "no-cache"
    response.headers["Content-Security-Policy"] = f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://*.oss-{settings.region}.aliyuncs.com; frame-ancestors 'self' capacitor://localhost http://localhost"
    return response


def connect(public: bool = False) -> oss2.Bucket:
    name, access_id, secret = settings.credentials.read_text().strip().split(":", 2)
    endpoint = f"https://oss-{settings.region}{'' if public else '-internal'}.aliyuncs.com"
    return oss2.Bucket(oss2.AuthV4(access_id, secret), endpoint, name, region=settings.region)


def database() -> sqlite3.Connection:
    settings.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(settings.data / "imports.sqlite", timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL, document TEXT NOT NULL, updated REAL NOT NULL)")
    return db


def save(job: dict[str, Any]) -> None:
    with database() as db:
        db.execute("INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?, ?)", (job["id"], job["owner"], job["fingerprint"], json.dumps(job, ensure_ascii=False), time.time()))


def abs_request(method: str, path: str, token: str, **kwargs: Any) -> httpx.Response:
    with httpx.Client(timeout=120) as client:
        result = client.request(method, settings.abs_url + path, headers={"Authorization": token}, **kwargs)
    if result.status_code >= 400:
        raise HTTPException(result.status_code if result.status_code < 500 else 502, "书库请求失败，请检查连接或重新登录")
    return result


def identity(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录")
    user = abs_request("GET", "/api/me", authorization).json()
    if user.get("type") not in {"root", "admin"}:
        raise HTTPException(403, "个人管理功能需要管理员账号")
    return {"id": user["id"], "token": authorization}


User = Annotated[dict[str, Any], Depends(identity)]


def job_for(job_id: str, user: dict[str, Any]) -> dict[str, Any]:
    with database() as db:
        row = db.execute("SELECT document FROM imports WHERE id=? AND owner=?", (job_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "导入任务不存在")
    return json.loads(row[0])


def file_for(job: dict[str, Any], file_id: str) -> dict[str, Any]:
    result = next((file for file in job["files"] if file["id"] == file_id), None)
    if result is None:
        raise HTTPException(404, "文件不存在")
    return result


def lock_for(job_id: str) -> threading.Lock:
    with locks_guard:
        return locks.setdefault(job_id, threading.Lock())


class FileSpec(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    size: int = Field(gt=0, le=50 * 1024**3)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        if name != safe_name(name) or PurePosixPath(name).suffix.lower() not in AUDIO | IMAGES:
            raise ValueError("请选择音频或封面文件，文件名不能包含路径")
        return name


class ImportSpec(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    author: str = Field(default="", max_length=120)
    narrator: str = Field(default="", max_length=120)
    series: str = Field(default="", max_length=120)
    library_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    files: list[FileSpec] = Field(min_length=1, max_length=3000)

    @field_validator("title")
    @classmethod
    def validate_title(cls, title: str) -> str:
        safe_name(title)
        return title.strip()


class Login(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


@app.post("/personal/api/login")
def login(body: Login) -> dict[str, str]:
    response = abs_request("POST", "/login", "", json=body.model_dump())
    user = response.json()["user"]
    if user.get("type") not in {"root", "admin"}:
        raise HTTPException(403, "需要管理员账号")
    return {"token": user.get("accessToken") or user["token"]}


@app.get("/personal/api/libraries")
def libraries(user: User) -> dict[str, Any]:
    result = abs_request("GET", "/api/libraries", user["token"]).json()
    return {"libraries": [lib for lib in result["libraries"] if lib["mediaType"] == "book" and any(folder["fullPath"] == "/audiobooks" for folder in lib["folders"])]}


@app.post("/personal/api/analyze")
def analyze_files(body: list[str], user: User) -> dict[str, object]:
    if len(body) > 3000 or any(len(name) > 240 for name in body):
        raise HTTPException(422, "文件过多或名称过长")
    return analyze(body)


@app.post("/personal/api/imports")
def create_import(body: ImportSpec, user: User) -> dict[str, Any]:
    safe_name(body.title)
    if body.library_id not in {lib["id"] for lib in libraries(user)["libraries"]}:
        raise HTTPException(403, "目标书库未配置到有声书存储")
    if not any(PurePosixPath(file.name).suffix.lower() in AUDIO for file in body.files):
        raise HTTPException(422, "至少需要一个音频文件")
    if analyze([file.name for file in body.files])["duplicateNames"]:
        raise HTTPException(409, "文件名重复，请先修改")
    spec = body.model_dump()
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with database() as db:
        row = db.execute("SELECT document FROM imports WHERE owner=? AND fingerprint=? ORDER BY updated DESC LIMIT 1", (user["id"], fingerprint)).fetchone()
        if row:
            existing = json.loads(row[0])
            if existing["state"] == "complete":
                try:
                    abs_request("GET", f"/api/items/{existing['bookId']}", user["token"])
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                    existing["state"] = "cancelled"
            if existing["state"] != "cancelled":
                return existing
    bucket = connect()
    job_id = uuid4().hex
    files = []
    try:
        for index, file in enumerate(body.files):
            file_id = uuid4().hex
            key = f"audiobookshelf/.imports/{job_id}/{file_id}"
            upload_id = bucket.init_multipart_upload(key).upload_id
            suffix = PurePosixPath(file.name).suffix.lower()
            destination = f"{index + 1:04d}-{file.name}" if suffix in AUDIO else file.name
            files.append({**file.model_dump(), "id": file_id, "key": key, "uploadId": upload_id, "destination": destination, "complete": False})
    except oss2.exceptions.OssError:
        for file in files:
            bucket.abort_multipart_upload(file["key"], file["uploadId"])
        raise HTTPException(502, "暂时无法创建上传，请稍后重试")
    job = {"id": job_id, "owner": user["id"], "fingerprint": fingerprint, "state": "uploading", "partSize": PART_SIZE, "metadata": {"title": body.title, "authors": [body.author] if body.author else [], "narrators": [body.narrator] if body.narrator else [], "series": [body.series] if body.series else []}, "libraryId": body.library_id, "destination": f"audiobookshelf/audiobooks/{safe_name(body.title)} [{job_id[:8]}]", "files": files}
    save(job)
    return job


@app.get("/personal/api/imports/{job_id}")
def import_status(job_id: str, user: User) -> dict[str, Any]:
    job = job_for(job_id, user)
    bucket = connect()
    for file in job["files"]:
        if not file["complete"] and bucket.object_exists(file["key"]):
            if bucket.head_object(file["key"]).content_length == file["size"]:
                file["complete"] = True
                save(job)
        file["parts"] = [] if file["complete"] else [{"number": part.part_number, "size": part.size} for part in oss2.PartIterator(bucket, file["key"], file["uploadId"])]
    return job


def valid_part(file: dict[str, Any], number: int) -> int:
    if file["complete"] or number < 1 or number > math.ceil(file["size"] / PART_SIZE):
        raise HTTPException(409, "分片编号或文件状态无效")
    return min(PART_SIZE, file["size"] - (number - 1) * PART_SIZE)


@app.post("/personal/api/imports/{job_id}/files/{file_id}/parts/{number}/url")
def part_url(job_id: str, file_id: str, number: int, user: User) -> dict[str, Any]:
    job = job_for(job_id, user)
    file = file_for(job, file_id)
    size = valid_part(file, number)
    url = connect(public=True).sign_url("PUT", file["key"], 900, params={"uploadId": file["uploadId"], "partNumber": str(number)}, slash_safe=True)
    return {"url": url, "size": size}


@app.put("/personal/api/imports/{job_id}/files/{file_id}/parts/{number}")
async def proxy_part(job_id: str, file_id: str, number: int, request: Request, user: User) -> dict[str, bool]:
    """Bounded fallback for browsers without OSS CORS; never buffers a whole book."""
    file = file_for(job_for(job_id, user), file_id)
    expected = valid_part(file, number)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > expected:
            raise HTTPException(413, "分片大小超出限制")
    if len(content) != expected:
        raise HTTPException(422, "分片不完整，请重试")
    from starlette.concurrency import run_in_threadpool
    await run_in_threadpool(connect().upload_part, file["key"], file["uploadId"], number, bytes(content))
    return {"ok": True}


@app.post("/personal/api/imports/{job_id}/files/{file_id}/complete")
def complete_file(job_id: str, file_id: str, user: User) -> dict[str, bool]:
    with lock_for(job_id):
        job = job_for(job_id, user)
        file = file_for(job, file_id)
        if file["complete"]:
            return {"ok": True}
        bucket = connect()
        # A previous request may have committed to OSS before its response was lost.
        if not bucket.object_exists(file["key"]):
            parts = list(oss2.PartIterator(bucket, file["key"], file["uploadId"]))
            if [p.part_number for p in parts] != list(range(1, math.ceil(file["size"] / PART_SIZE) + 1)) or any(p.size != valid_part(file, p.part_number) for p in parts):
                raise HTTPException(409, "还有分片没有上传完成")
            bucket.complete_multipart_upload(file["key"], file["uploadId"], [PartInfo(part.part_number, part.etag) for part in parts])
        if bucket.head_object(file["key"]).content_length != file["size"]:
            raise HTTPException(409, "文件大小校验失败")
        file["complete"] = True
        save(job)
    return {"ok": True}


@app.post("/personal/api/imports/{job_id}/complete")
def complete_import(job_id: str, user: User) -> dict[str, Any]:
    with lock_for(job_id):
        job = job_for(job_id, user)
        if job["state"] == "complete":
            return job
        if not all(file["complete"] for file in job["files"]):
            raise HTTPException(409, "请先完成所有文件上传")
        bucket = connect()
        job["state"] = "publishing"
        save(job)
        for file in job["files"]:
            destination = f"{job['destination']}/{file['destination']}"
            copy_object(bucket, file["key"], destination, file["size"])
            if bucket.head_object(destination).content_length != file["size"]:
                raise HTTPException(502, "入库校验失败，可以安全重试")
        bucket.put_object(f"{job['destination']}/metadata.json", json.dumps(job["metadata"], ensure_ascii=False).encode())
        abs_request("POST", f"/api/libraries/{job['libraryId']}/scan", user["token"])
        finish_catalog(job, user)
        job["state"] = "complete"
        save(job)
        for file in job["files"]:
            bucket.delete_object(file["key"])
        return job


@app.get("/personal/api/books")
def books(user: User) -> dict[str, Any]:
    result = []
    for library in libraries(user)["libraries"]:
        page = 0
        count = 0
        while True:
            data = abs_request("GET", f"/api/libraries/{library['id']}/items", user["token"], params={"limit": 100, "page": page}).json()
            result.extend(data["results"])
            count += len(data["results"])
            if count >= data.get("total", 0) or not data["results"]:
                break
            page += 1
    return {"books": result}


def finish_catalog(job: dict[str, Any], user: dict[str, Any]) -> None:
    relative = job["destination"].removeprefix("audiobookshelf/audiobooks/")
    for attempt in range(45):
        item = next((book for book in books(user)["books"] if book.get("relPath") == relative), None)
        if item:
            expanded = abs_request("GET", f"/api/items/{item['id']}?expanded=1", user["token"]).json()
            audio = expanded["media"]["audioFiles"]
            by_name = {file["metadata"]["filename"]: file for file in audio}
            expected = [file["destination"] for file in job["files"] if PurePosixPath(file["name"]).suffix.lower() in AUDIO]
            if all(name in by_name for name in expected):
                # Explicit manual order wins over embedded MP3 track tags on future scans.
                abs_request("PATCH", f"/api/items/{item['id']}/tracks", user["token"], json={"orderedFileData": [{"ino": by_name[name]["ino"], "exclude": False} for name in expected]})
                job["bookId"] = item["id"]
                return
        time.sleep(1)
    raise HTTPException(409, "文件已保存，书库仍在扫描。稍后点击上传可继续完成入库，无需重新传输。")


def copy_object(bucket: oss2.Bucket, source: str, destination: str, size: int) -> None:
    if size < 1024**3:
        bucket.copy_object(bucket.bucket_name, source, destination)
        return
    upload_id = bucket.init_multipart_upload(destination).upload_id
    parts = []
    try:
        for index, offset in enumerate(range(0, size, 100 * 1024**2), 1):
            copied = bucket.upload_part_copy(bucket.bucket_name, source, (offset, min(size, offset + 100 * 1024**2) - 1), destination, upload_id, index)
            parts.append(PartInfo(index, copied.etag))
        bucket.complete_multipart_upload(destination, upload_id, parts)
    except oss2.exceptions.OssError:
        bucket.abort_multipart_upload(destination, upload_id)
        raise


class MetadataEdit(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    author: str | None = Field(default=None, max_length=120)
    narrator: str | None = Field(default=None, max_length=120)
    series: str | None = Field(default=None, max_length=120)


@app.patch("/personal/api/books")
def edit_books(body: MetadataEdit, user: User) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field, target in (("author", "authors"), ("narrator", "narrators"), ("series", "series")):
        value = getattr(body, field)
        if value is not None:
            metadata[target] = ([{"name": value}] if target in {"authors", "series"} else [value]) if value else []
    if not metadata:
        raise HTTPException(422, "请选择要修改的字段")
    allowed = {book["id"] for book in books(user)["books"]}
    if any(item not in allowed for item in body.ids):
        raise HTTPException(403, "所选书籍不在有声书库中")
    updated = []
    for item in body.ids:
        abs_request("PATCH", f"/api/items/{item}/media", user["token"], json={"metadata": metadata})
        updated.append(item)
    return {"updated": updated}


@app.get("/personal/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/personal/api/backups")
def backup_status(user: User) -> dict[str, Any]:
    path = settings.data / "last-backup.json"
    return {"last": json.loads(path.read_text()) if path.exists() else None, "retention": 14}


@app.post("/personal/api/backups")
def backup_now(user: User) -> dict[str, object]:
    from backups import create_backup
    with lock_for("backup"):
        return create_backup(user["token"])


@app.get("/personal/{asset:path}")
def static_asset(asset: str) -> FileResponse:
    root = Path(__file__).parent / "web"
    path = (root / (asset or "index.html")).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)
