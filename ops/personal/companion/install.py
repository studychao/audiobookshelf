"""Install only the personal companion and its Caddy route on the existing host."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def main() -> None:
    root = Path(__file__).resolve().parent
    data = Path("/opt/audiobookshelf/personal-data")
    data.mkdir(mode=0o700, exist_ok=True)
    gateway = json.loads(run("docker", "network", "inspect", "one-xhs-digest_edge"))[0]["IPAM"]["Config"][0]["Gateway"]
    (root / "runtime.env").write_text(f"PERSONAL_BIND={gateway}\n")
    for name in ("audiobookshelf-personal.service", "audiobookshelf-backup.service", "audiobookshelf-backup.timer"):
        shutil.copyfile(root / name, Path("/etc/systemd/system") / name)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "audiobookshelf-personal.service")
    run("systemctl", "restart", "audiobookshelf-personal.service")
    caddy = Path("/opt/one-xhs-digest/Caddyfile")
    old = caddy.read_text()
    route = f"""    redir /personal /personal/
    handle /personal/* {{
        reverse_proxy {gateway}:13379
    }}
    handle {{
        reverse_proxy audiobookshelf:80
    }}"""
    if "handle /personal/*" not in old:
        replacement = old.replace("    reverse_proxy audiobookshelf:80", route)
        if replacement == old:
            raise RuntimeError("Expected Audiobookshelf route was not found; Caddy left unchanged")
        backup = Path("/opt/audiobookshelf") / f"Caddyfile.before-personal-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        shutil.copyfile(caddy, backup)
        caddy.write_text(replacement)
        try:
            run("docker", "exec", "one-xhs-caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
        except subprocess.CalledProcessError:
            caddy.write_text(old)
            raise
        run("docker", "restart", "one-xhs-caddy")
    run("systemctl", "enable", "--now", "audiobookshelf-backup.timer")
    print("Personal service and daily verified backup timer installed")


if __name__ == "__main__":
    main()
