"""Initialize a new local-only server before publishing its HTTPS endpoint."""
from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path("/opt/audiobookshelf")


def command(args: list[str]) -> str:
    return subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=True).stdout


def request(path: str, data: dict[str, object] | None = None) -> object:
    body = json.dumps(data).encode() if data is not None else None
    req = Request(f"http://127.0.0.1:13378{path}", data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as response:
        text = response.read().decode()
        return json.loads(text) if response.headers.get_content_type() == "application/json" else text


def main() -> None:
    if not (ROOT / "docker-compose.yml").is_file():
        raise RuntimeError("Run on the configured server after staging deployment files")
    active = subprocess.run(["systemctl", "is-active", "--quiet", "audiobookshelf.service"], check=False)
    if active.returncode == 0:
        raise RuntimeError("The final service is already active; bootstrap is only for first installation")
    for name in ("config", "metadata"):
        folder = ROOT / name
        folder.mkdir(exist_ok=True)
        shutil.chown(folder, user=1000, group=1000)
    private = ROOT / "secrets"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)

    # No media directories or public port are exposed while initializing.
    config = json.loads(command(["docker", "compose", "config", "--format", "json"]))
    service = config["services"]["audiobookshelf"]
    service["volumes"] = [v for v in service["volumes"] if v["target"] in {"/config", "/metadata"}]
    (ROOT / "bootstrap-compose.json").write_text(json.dumps(config, indent=2))
    command(["docker", "compose", "-p", "audiobookshelf", "-f", "bootstrap-compose.json", "up", "-d"])
    for attempt in range(30):
        try:
            status = request("/status")
            break
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 29:
                raise RuntimeError("Audiobookshelf did not start within 60 seconds") from None
            time.sleep(2)
    credentials_path = private / "admin.json"
    if not status["isInit"]:
        if credentials_path.exists():
            credentials = json.loads(credentials_path.read_text())
        else:
            credentials = {"username": "chao", "password": secrets.token_urlsafe(18)}
            credentials_path.write_text(json.dumps(credentials))
            credentials_path.chmod(0o600)
        request("/init", {"newRoot": credentials})
    if not credentials_path.exists():
        raise RuntimeError("Existing server: credentials unavailable; refusing to change authentication")
    login = request("/login", json.loads(credentials_path.read_text()))
    if not isinstance(login, dict) or not login.get("user"):
        raise RuntimeError("Local authentication verification failed")
    if not request("/status")["isInit"]:
        raise RuntimeError("Server initialization did not persist")
    print("Server initialized; local login verified; no media library created.")

    caddy = Path("/opt/one-xhs-digest/Caddyfile")
    fragment = (ROOT / "Caddyfile.fragment").read_text()
    original = caddy.read_text()
    domain = fragment.strip().split()[0]
    if domain not in original:
        candidate = ROOT / "Caddyfile.candidate"
        candidate.write_text(original.rstrip() + "\n" + fragment)
        command(["docker", "cp", str(candidate), "one-xhs-caddy:/tmp/Caddyfile.audiobookshelf"])
        command(["docker", "exec", "one-xhs-caddy", "caddy", "validate", "--config", "/tmp/Caddyfile.audiobookshelf", "--adapter", "caddyfile"])
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(caddy, ROOT / f"Caddyfile.before-{stamp}")
        caddy.write_text(candidate.read_text())
        try:
            command(["docker", "restart", "one-xhs-caddy"])
        except subprocess.CalledProcessError:
            caddy.write_text(original)
            command(["docker", "restart", "one-xhs-caddy"])
            raise
        print(f"HTTPS route configured for {domain}; previous Caddyfile backed up.")


if __name__ == "__main__":
    main()
