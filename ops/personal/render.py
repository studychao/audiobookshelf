"""Render an OSS-backed Audiobookshelf service; does not change the host."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class Deployment:
    bucket: str
    domain: str
    region: str = "us-west-1"
    image: str = "ghcr.io/advplyr/audiobookshelf:2.36.0@sha256:180acad33d69c99ed208676465d8edcb268fa46967735579a7810859885b1a8e"

    def __post_init__(self) -> None:
        for name, value in (("bucket", self.bucket), ("domain", self.domain), ("region", self.region)):
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
                raise ValueError(f"{name} must be a DNS hostname, got {value!r}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket):
            raise ValueError("bucket must be a valid OSS bucket name")
        if not re.fullmatch(r"[a-z]+-[a-z]+-\d+", self.region):
            raise ValueError("region must be an Alibaba Cloud region identifier")

    def files(self) -> dict[str, str]:
        return {
            "docker-compose.yml": f"""services:
  audiobookshelf:
    image: {self.image}
    container_name: audiobookshelf
    user: "1000:1000"
    init: true
    restart: "no"
    environment:
      TZ: Asia/Shanghai
    ports:
      - "127.0.0.1:13378:80"
    volumes:
      - type: bind
        source: /mnt/audiobookshelf/audiobooks
        target: /audiobooks
        bind:
          create_host_path: false
      - type: bind
        source: /mnt/audiobookshelf/podcasts
        target: /podcasts
        bind:
          create_host_path: false
      - /opt/audiobookshelf/config:/config
      - /opt/audiobookshelf/metadata:/metadata
    networks:
      - edge
    mem_limit: 512m
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://127.0.0.1/healthcheck').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
networks:
  edge:
    external: true
    name: one-xhs-digest_edge
""",
            "audiobookshelf-storage.service": f"""[Unit]
Description=Audiobookshelf private OSS storage
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStartPre=/usr/bin/test -s /opt/audiobookshelf/secrets/oss-passwd
ExecStartPre=/usr/bin/mkdir -p /mnt/audiobookshelf /var/cache/audiobookshelf-oss
ExecStart=/usr/local/bin/ossfs {self.bucket}:/audiobookshelf /mnt/audiobookshelf -f -o url=https://oss-{self.region}-internal.aliyuncs.com -o sigv4 -o region={self.region} -o passwd_file=/opt/audiobookshelf/secrets/oss-passwd -o allow_other -o uid=1000 -o gid=1000 -o umask=0022 -o fsname={self.bucket}:/audiobookshelf -o subtype=ossfs -o tmpdir=/var/cache/audiobookshelf-oss -o ensure_diskfree=5120 -o parallel_count=2 -o max_stat_cache_size=1000 -o stat_cache_expire=30 -o nosuid -o nodev
ExecStartPost=/usr/bin/python3 /opt/audiobookshelf/guard.py --mount-only --wait
ExecStop=/usr/bin/fusermount -u /mnt/audiobookshelf
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
MemoryMax=256M
""",
            "audiobookshelf.service": """[Unit]
Description=Audiobookshelf with required OSS storage
Requires=docker.service audiobookshelf-storage.service
After=docker.service audiobookshelf-storage.service network-online.target
BindsTo=audiobookshelf-storage.service

[Service]
Type=simple
WorkingDirectory=/opt/audiobookshelf
ExecStartPre=/usr/bin/python3 /opt/audiobookshelf/guard.py
ExecStart=/usr/bin/docker compose up --no-build --pull never --abort-on-container-exit
ExecStop=/usr/bin/docker compose stop --timeout 30
Restart=on-failure
RestartSec=10
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
""",
            "guard.py": f'''"""Refuse to start if media is not on the configured private OSS mount."""
import json
from pathlib import Path
import subprocess
import sys
import time

for attempt in range(40 if "--wait" in sys.argv else 1):
    result = subprocess.run(
        ["findmnt", "--json", "--mountpoint", "/mnt/audiobookshelf", "--output", "SOURCE,FSTYPE"],
        check=False, capture_output=True, text=True,
    )
    mounts = json.loads(result.stdout).get("filesystems", []) if result.returncode == 0 else []
    if len(mounts) == 1 and mounts[0]["fstype"] == "fuse.ossfs" and mounts[0]["source"] == "{self.bucket}:/audiobookshelf":
        break
    if "--wait" not in sys.argv or attempt == 39:
        raise RuntimeError("Expected OSS is not mounted; refusing to use local media directories")
    time.sleep(0.5)
if "--mount-only" not in sys.argv:
    for name in ("audiobooks", "podcasts"):
        folder = Path("/mnt/audiobookshelf") / name
        if not folder.is_dir() or folder.is_symlink():
            raise RuntimeError(f"OSS media directory is missing or is a symlink: {{folder}}")
''',
            "Caddyfile.fragment": f"""\n{self.domain} {{
    encode zstd gzip
    header {{
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
        -Server
    }}
    reverse_proxy audiobookshelf:80
}}
""",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="us-west-1")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    deployment = Deployment(args.bucket, args.domain, args.region)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, content in deployment.files().items():
        (args.output / name).write_text(content)
    print(f"Rendered {len(deployment.files())} files to {args.output}")


if __name__ == "__main__":
    main()
