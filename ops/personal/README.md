# Personal OSS deployment

Audiobookshelf uses the pinned official 2.36.0 server with a customized web client layered on top. The Python files render and verify the personal deployment; upstream server behavior is unchanged.

- OSS media: `BUCKET/audiobookshelf/audiobooks/` and `BUCKET/audiobookshelf/podcasts/`.
- Server mount: `/mnt/audiobookshelf`, using ossfs 1.91.11 with V4 authentication and a same-region internal HTTPS endpoint.
- Database and metadata: `/opt/audiobookshelf/config` and `/opt/audiobookshelf/metadata`, on local disk.
- Credentials: `/opt/audiobookshelf/secrets/oss-passwd` and `admin.json`, mode 0600, inside a mode 0700 directory. Never commit credentials.

The existing Caddy container and `one-xhs-digest_edge` Docker network are reused. Caddy gets a dedicated hostname and a backup of its previous configuration. Bootstrap initializes and verifies the administrator account before exposing HTTPS. It is only for first installation.

## Generate configuration

```sh
python3 ops/personal/render.py --bucket YOUR_BUCKET --region us-west-1 \
  --domain books.example.com --output /tmp/audiobookshelf-deployment
python3 -m pytest ops/personal/test_render.py -q
```

Stage the generated files under `/opt/audiobookshelf`. Install ossfs and its FUSE library from the official Alibaba Cloud Ubuntu package, and install the Python `oss2` SDK in a dedicated virtual environment. `prepare_oss.py` creates only the `audiobookshelf/` prefix after authenticating with the private credentials file.

Install `audiobookshelf-storage.service` and `audiobookshelf.service` under `/etc/systemd/system`, then:

```sh
systemctl daemon-reload
systemctl enable --now audiobookshelf.service
```

## Web navigation

The web app toolbar includes a visible “添加书籍” link to `/personal/` for administrators. It reuses the same-origin login token and leaves the original `/upload` entry available. On narrow screens the new entry uses an accessible plus icon.

Build the customized client from the locked dependencies, then layer its static assets on the pinned server image:

```sh
cd client
CYPRESS_INSTALL_BINARY=0 npm ci --no-audit --no-fund
npm run generate
cd ..
docker build -f ops/personal/Dockerfile.web -t audiobookshelf-personal:web-COMMIT .
```

On the server, back up `/opt/audiobookshelf/docker-compose.yml`, set only its image to the newly built tag, validate with `docker compose config --quiet`, and restart `audiobookshelf.service`. Keep the old image and Compose backup for rollback. Do not bypass the existing OSS mount guard. The Dockerfile copies only web assets; database, media mounts and the server runtime stay on the pinned version.

## Storage behavior

The application starts only after the expected OSS FUSE mount is ready. Missing or incorrect storage prevents startup. Docker may not create missing media directories, and it does not restart independently of systemd. A storage service failure stops the application. After fixing a mount problem, run `systemctl restart audiobookshelf.service`.

ossfs uses temporary local files for reads and writes. `ensure_diskfree=5120` reserves 5 GiB on the server filesystem; very large simultaneous files can still exceed the available working space. Media are persisted in OSS, not the temporary directory. This is suitable for a personal audiobook library, not a substitute for unrestricted NAS filesystem semantics.

External changes to an OSS bucket do not reliably produce local filesystem notifications. Libraries disable the watcher and scan every 15 minutes; an immediate scan is available in the web interface. Upload each book as its own folder under `audiobooks/`. Avoid editing the same file concurrently through different clients.

## Verification

```sh
/opt/audiobookshelf/venv/bin/python /opt/audiobookshelf/verify.py \
  --url https://books.example.com
```

The verification creates missing book/podcast libraries, uploads a unique synthetic WAV, compares SHA-256 against the private OSS object, tests a random mounted read and HTTP 206 range playback, opens/closes a playback session, then removes only that synthetic item. It also checks authentication and the Socket.IO transport. The resulting `verification.json` contains no credentials.

The local database and metadata are separate from OSS media. Include them in server backups; media alone does not preserve listening history or library configuration.
