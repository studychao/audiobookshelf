# Personal audiobook tools

Adds `/personal/` alongside the unchanged Audiobookshelf 2.36 server. The Python service authenticates each API request against ABS and requires an admin/root account. ABS continues to own users, metadata, playback and its SQLite database.

## What is included

- One-book imports from desktop, iOS Files, or the native share inbox: title, author, narrator, series, audio, ebooks and cover files. EPUB, PDF, MOBI, AZW3, CBZ and CBR uploads are supported. Import an ebook alone or alongside its audio. When multiple ebook versions belong to the same book, select the default reading version (EPUB is preferred automatically). Other versions stay as supplementary files.
- Chinese/Arabic chapter numbers and volume-aware sorting, missing/duplicate warnings, and manual ordering. ABS tracks are explicitly marked as manually ordered so embedded MP3 tags cannot undo that order.
- OSS V4 signed multipart uploads, 4 MiB parts, persisted task state and a bounded proxy fallback. Re-select the same files and metadata to resume. A repeated completed import returns the same book; a deleted book can be imported again.
- Batch author/narrator/series editing. Only checked fields change; a checked empty field explicitly clears that field.
- Daily private OSS backups, SHA-256 verification after upload, ZIP CRC and SQLite integrity checks, and retention of 14 successfully created archives.

The target ABS library must have `audiobooksOnly` disabled. The existing personal library already does. An import requires at least one audio or ebook file; covers alone are rejected. Ebooks keep their original filenames and never participate in audio chapter numbering. The primary ebook is explicitly verified after scanning, and ebook-only imports do not call the audio track-order API.

The client fingerprint samples the first/last 64 KiB plus file name and size to identify a resume request; it is not a full-file integrity checksum. Multipart completion checks every part and final object size. `verify_live.py` additionally compares the full generated test audio SHA-256 against OSS.

## Deployment

The existing host has `/opt/audiobookshelf/venv`, private OSS credentials in `/opt/audiobookshelf/secrets/oss-passwd` (`bucket:access-id:secret`), and an ABS admin login JSON in `/opt/audiobookshelf/secrets/admin.json`. Keep these files mode 0600 and outside source control.

```sh
/opt/audiobookshelf/venv/bin/pip install -r requirements.txt
/opt/audiobookshelf/venv/bin/python install.py
```

`install.py` targets this deployment's `one-xhs-digest_edge` Docker network and `/opt/one-xhs-digest/Caddyfile`. It backs up Caddy, validates before restarting, and preserves the existing One route. The companion binds to the bridge gateway on port 13379, and Caddy exposes only `/personal/*`. Database/config remain on local disk; audio and ebooks remain under the private bucket prefix `audiobookshelf/audiobooks/`.

Environment overrides: `PERSONAL_DATA`, `OSS_CREDENTIALS`, `ABS_URL`, `OSS_REGION`, `PUBLIC_ORIGIN`, and service `PERSONAL_BIND`. Runtime environment files and private files must not be committed.

OSS CORS is configured on `one-chao`:

- Origin: `https://audiobook.47-77-238-23.sslip.io`
- Method: `PUT`
- Allowed headers: `Content-Type`, `Content-MD5`
- Exposed response header: `ETag`
- Max age: 600 seconds; Vary Origin enabled.

The bucket remains private. Browsers receive short-lived upload URLs, never RAM keys. Without CORS, uploads fall back to the authenticated companion.

## Backup and recovery

`audiobookshelf-backup.timer` runs at 19:15 UTC (03:15 Asia/Shanghai), with up to five minutes of random delay. Manual and scheduled backups use the same OS file lock. Archives and manifests use OSS server-side AES256 encryption. The archive contains the ABS database and metadata; audio and ebook files remain in their original OSS location. This is a metadata backup, not an independent second copy of the audio/ebook objects.

```sh
systemctl status audiobookshelf-personal audiobookshelf-backup.timer
systemctl start audiobookshelf-backup
journalctl -u audiobookshelf-personal -u audiobookshelf-backup
```

To recover:

1. Read `/opt/audiobookshelf/personal-data/last-backup.json` and select the desired archive key.
2. Download and verify it without changing the running server:
   ```sh
   /opt/audiobookshelf/venv/bin/python backups.py \
     --check-restore audiobookshelf/backups/SELECTED_TIMESTAMP.audiobookshelf \
     --output /tmp/restore.audiobookshelf
   ```
3. Keep a fresh backup of the current state. On a compatible ABS version, open Settings → Backups, upload the verified `.audiobookshelf` file, and apply it. Applying a backup replaces users, metadata and listening progress with the selected snapshot.
4. Keep the existing audio library mounted. Verify login, book count, playback position and one audio playback after restoring.

`verify_restore.py` performs a non-destructive rehearsal: it downloads the last backup, extracts only the DB/metadata to a temporary directory, starts the current pinned ABS image with `--network none` and empty temporary media directories, verifies login/library IDs/progress count, and removes the temporary container and directory. Production DB and media are never mounted into that container. This checks metadata recovery; it does not play production audio inside the isolated container.

## Validation

```sh
python -m pytest -q test_companion.py
node --test web/import-client.test.mjs
# Run only on the configured personal server:
/opt/audiobookshelf/venv/bin/python verify_live.py --url https://audiobook.47-77-238-23.sslip.io
/opt/audiobookshelf/venv/bin/python verify_restore.py
/opt/audiobookshelf/venv/bin/python verify_ebooks.py
```

The live test creates a clearly named synthetic book, exercises public OSS direct upload, restart/resume, proxy upload, scan, metadata editing and HTTP Range playback, then removes only that generated book. Reports contain no credentials and are stored in `personal-data/verification.json` and `restore-verification.json`.

Interrupted imports are retained to support resuming. No lifecycle rule automatically deletes their multipart uploads; abandoned import storage can be reviewed under `audiobookshelf/.imports/` in OSS. Avoid deleting a task that is still uploading.

The ebook verification uses generated EPUB/PDF content in three cases: EPUB-only, PDF-only, and audio + EPUB + PDF with PDF explicitly selected as primary. It checks signed uploads, catalog registration, exact bytes from the reader endpoint, full OSS SHA-256 and reading-progress persistence; it removes only its generated books afterward. Results are in `personal-data/ebook-verification.json`.
