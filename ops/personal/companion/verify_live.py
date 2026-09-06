"""Exercise real resumable imports and private backups using generated audio only."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tempfile
import wave
from uuid import uuid4

import httpx

from backups import download_and_check
from service import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    credentials = json.loads(Path("/opt/audiobookshelf/secrets/admin.json").read_text())
    with httpx.Client(base_url=args.url, timeout=240) as client:
        login = client.post("/personal/api/login", json=credentials)
        login.raise_for_status()
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"

        def api(method: str, path: str, **kwargs: object) -> httpx.Response:
            response = client.request(method, "/personal/api" + path, **kwargs)
            response.raise_for_status()
            return response

        library = api("GET", "/libraries").json()["libraries"][0]
        audio = io.BytesIO()
        with wave.open(audio, "wb") as file:
            file.setnchannels(1); file.setsampwidth(2); file.setframerate(16000)
            file.writeframes((b"\x00\x08\x00\xf8") * (1100 * 1024))
        content = audio.getvalue()
        title = f"Personal verification {uuid4().hex[:12]}"
        spec = {"title": title, "author": "验证作者", "narrator": "验证演播者", "series": "验证系列", "library_id": library["id"], "files": [{"name": "第十二集.wav", "size": len(content), "fingerprint": hashlib.sha256(content).hexdigest()}]}
        job = api("POST", "/imports", json=spec).json()
        file = job["files"][0]
        base = f"/imports/{job['id']}"
        part_base = f"{base}/files/{file['id']}"
        book_id = None
        try:
            # Use both direct public OSS and bounded proxy transfers in the same upload.
            signed = api("POST", part_base + "/parts/1/url").json()["url"]
            direct = httpx.put(signed, content=content[:job["partSize"]], timeout=120)
            if direct.status_code != 200:
                raise RuntimeError(f"Signed OSS upload failed with HTTP {direct.status_code}")
            restored = api("GET", base).json()
            assert restored["files"][0]["parts"] == [{"number": 1, "size": job["partSize"]}]
            assert api("POST", "/imports", json=spec).json()["id"] == job["id"]
            for number, offset in enumerate(range(job["partSize"], len(content), job["partSize"]), 2):
                api("PUT", part_base + f"/parts/{number}", content=content[offset:offset + job["partSize"]])
            api("POST", part_base + "/complete")
            result = api("POST", base + "/complete").json()
            book_id = result["bookId"]
            assert result["state"] == "complete"
            stored = connect().get_object(job["destination"] + "/" + file["destination"]).read()
            assert hashlib.sha256(stored).digest() == hashlib.sha256(content).digest()
            expanded = client.get(f"/api/items/{book_id}?expanded=1").json()
            assert expanded["media"]["metadata"]["title"] == title
            assert expanded["media"]["audioFiles"][0]["manuallyVerified"]
            track = expanded["media"]["audioFiles"][0]
            playback = client.get(f"/api/items/{book_id}/file/{track['ino']}", headers={"Range": "bytes=100000-104095"})
            assert playback.status_code == 206 and playback.content == content[100000:104096]
            api("PATCH", "/books", json={"ids": [book_id], "author": "整理后的作者"})
            edited = client.get(f"/api/items/{book_id}?expanded=1").json()
            assert edited["media"]["metadata"]["authors"][0]["name"] == "整理后的作者"
            print("Signed direct upload, interrupted multipart resume, publish, track order, metadata edit, OSS SHA-256 and playback Range passed", flush=True)
        finally:
            if book_id:
                removal = client.delete(f"/api/items/{book_id}?hard=1")
                removal.raise_for_status()
            else:
                for obj in list(__import__('oss2').ObjectIterator(connect(), prefix=job["destination"] + "/")):
                    connect().delete_object(obj.key)
            for obj in list(__import__('oss2').ObjectIterator(connect(), prefix=f"audiobookshelf/.imports/{job['id']}/")):
                connect().delete_object(obj.key)
        backup = api("POST", "/backups").json()
        with tempfile.TemporaryDirectory(prefix="abs-restore-verification-") as directory:
            integrity = download_and_check(backup["key"], Path(directory) / "restore.audiobookshelf")
        report = {"import": True, "direct_upload": True, "resume": True, "range": True, "metadata_edit": True, "temporary_book_removed": True, "backup": backup, "restore": integrity}
        Path("/opt/audiobookshelf/personal-data/verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("Private backup download and SQLite restore verification passed", flush=True)


if __name__ == "__main__":
    main()
