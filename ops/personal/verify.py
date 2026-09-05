"""Verify authenticated uploads, OSS persistence, and seekable playback."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import time
from uuid import uuid4
import wave

import requests

from prepare_oss import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("Verification requires the public HTTPS server URL")
    credentials = json.loads(Path("/opt/audiobookshelf/secrets/admin.json").read_text())
    session = requests.Session()
    response = session.post(f"{base}/login", json=credentials, timeout=20)
    response.raise_for_status()
    user = response.json()["user"]
    token = user.get("accessToken") or user.get("token")
    if not token:
        raise RuntimeError("Login did not return an API token")
    session.headers["Authorization"] = f"Bearer {token}"

    def api(method: str, path: str, **kwargs: object) -> requests.Response:
        result = session.request(method, base + path, timeout=60, **kwargs)
        result.raise_for_status()
        return result

    libraries = api("GET", "/api/libraries").json()["libraries"]
    for name, media_type, folder in (("有声书", "book", "/audiobooks"), ("播客", "podcast", "/podcasts")):
        if not any(any(f["fullPath"] == folder for f in lib["folders"]) for lib in libraries):
            library = api("POST", "/api/libraries", json={
                "name": name, "mediaType": media_type, "folders": [{"fullPath": folder}],
                "settings": {"disableWatcher": True, "autoScanCronExpression": "*/15 * * * *"},
            }).json()
            libraries.append(library)
    book_library = next(lib for lib in libraries if lib["mediaType"] == "book")
    anonymous = requests.get(base + "/api/libraries", timeout=15)
    if anonymous.status_code != 401:
        raise RuntimeError(f"Expected private library access to return 401, got {anonymous.status_code}")
    handshake = api("GET", "/socket.io/?EIO=4&transport=polling")
    if not handshake.text.startswith("0{"):
        raise RuntimeError("Socket.IO handshake failed")

    title = f"Storage verification {uuid4().hex[:12]}"
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"".join(struct.pack("<h", (index % 127) * 100 - 6350) for index in range(16000 * 8)))
    data = output.getvalue()
    api("POST", "/api/upload", data={"title": title, "library": book_library["id"], "folder": book_library["folders"][0]["id"]}, files={"file": ("storage-check.wav", data, "audio/wav")})
    api("POST", f"/api/libraries/{book_library['id']}/scan")
    for attempt in range(30):
        items = api("GET", f"/api/libraries/{book_library['id']}/items?limit=100").json()["results"]
        item = next((item for item in items if item["media"]["metadata"]["title"] == title), None)
        if item:
            break
        if attempt == 29:
            raise RuntimeError("Uploaded verification audio was not scanned")
        time.sleep(1)
    expanded = api("GET", f"/api/items/{item['id']}?expanded=1").json()
    audio_file = next(file for file in expanded["libraryFiles"] if file["metadata"]["filename"] == "storage-check.wav")
    relative = Path(audio_file["metadata"]["path"]).relative_to("/audiobooks")
    key = f"audiobookshelf/audiobooks/{relative.as_posix()}"
    bucket = connect()
    stored = bucket.get_object(key).read()
    if hashlib.sha256(stored).digest() != hashlib.sha256(data).digest():
        raise RuntimeError("OSS contents do not match the uploaded audio")
    local_path = Path("/mnt/audiobookshelf/audiobooks") / relative
    with local_path.open("rb") as mounted:
        mounted.seek(100000)
        if mounted.read(4096) != data[100000:104096]:
            raise RuntimeError("Random read from OSS mount returned wrong bytes")

    url = f"/api/items/{item['id']}/file/{audio_file['ino']}"
    ranged = api("GET", url, headers={"Range": "bytes=1024-8191"})
    if ranged.status_code != 206 or ranged.content != data[1024:8192]:
        raise RuntimeError("HTTPS byte range playback failed")
    play = api("POST", f"/api/items/{item['id']}/play", json={"mediaPlayer": "storage-verification", "forceDirectPlay": True, "supportedMimeTypes": ["audio/wav"]}).json()
    if not play.get("audioTracks"):
        raise RuntimeError("Playback session did not return an audio track")
    api("POST", f"/api/session/{play['id']}/close", json={"currentTime": 0, "timeListened": 0})

    # Remove only the unique synthetic item created by this verification.
    api("DELETE", f"/api/items/{item['id']}?hard=1")
    if bucket.object_exists(key):
        raise RuntimeError("Verification audio was not removed from OSS")
    result = {
        "https": True, "authentication": True, "anonymous_library_status": 401,
        "socket_io": True, "upload": True, "oss_sha256_match": True,
        "random_read": True, "http_range_status": 206, "playback_session": True,
        "test_audio_removed": True,
        "libraries": [{"name": lib["name"], "id": lib["id"]} for lib in libraries],
    }
    Path("/opt/audiobookshelf/verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
