"""Restore the latest verified backup into an isolated container, never production."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4
import zipfile

from backups import download_and_check
from service import abs_request, settings

NODE_CHECK = """
let input = ''; process.stdin.on('data', b => input += b);
process.stdin.on('end', async () => {
  try {
    const response = await fetch('http://127.0.0.1/login', {method:'POST',headers:{'Content-Type':'application/json'},body:input});
    if (!response.ok) process.exit(2);
    const {user} = await response.json();
    const token = user.accessToken || user.token;
    const libraries = await fetch('http://127.0.0.1/api/libraries', {headers:{Authorization:'Bearer '+token}});
    if (!libraries.ok) process.exit(3);
    const data = await libraries.json();
    console.log(JSON.stringify({libraryIds:data.libraries.map(x=>x.id).sort(),progressCount:user.mediaProgress.length}));
  } catch { process.exit(4); }
});
"""


def main() -> None:
    credentials = json.loads(Path('/opt/audiobookshelf/secrets/admin.json').read_text())
    user = abs_request('POST', '/login', '', json=credentials).json()['user']
    token = 'Bearer ' + (user.get('accessToken') or user['token'])
    libraries = abs_request('GET', '/api/libraries', token).json()['libraries']
    expected = {'libraryIds': sorted(library['id'] for library in libraries), 'progressCount': len(user['mediaProgress'])}
    backup = json.loads((settings.data / 'last-backup.json').read_text())
    name = 'abs-restore-verification-' + uuid4().hex[:12]
    image = subprocess.check_output(['docker', 'inspect', '--format', '{{.Config.Image}}', 'audiobookshelf'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='abs-isolated-restore-') as folder:
        root = Path(folder)
        archive = root / 'restore.audiobookshelf'
        download_and_check(backup['key'], archive)
        config, metadata = root / 'config', root / 'metadata'
        config.mkdir(); metadata.mkdir()
        with zipfile.ZipFile(archive) as source:
            for entry in source.infolist():
                if entry.filename == 'absdatabase.sqlite':
                    target = config / entry.filename
                elif entry.filename.startswith(('metadata-items/', 'metadata-authors/')):
                    parts = PurePosixPath(entry.filename).parts
                    if '..' in parts or entry.filename.startswith('/'):
                        raise ValueError('Unsafe path in backup')
                    target = metadata / parts[0].removeprefix('metadata-') / Path(*parts[1:])
                else:
                    continue
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(entry) as data, target.open('wb') as output:
                        shutil.copyfileobj(data, output)
        try:
            subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'none', '--memory', '256m', '--cpus', '0.5', '-v', f'{config}:/config', '-v', f'{metadata}:/metadata', '--tmpfs', '/audiobooks', '--tmpfs', '/podcasts', image], check=True, stdout=subprocess.DEVNULL)
            for attempt in range(60):
                check = subprocess.run(['docker', 'exec', '-i', name, 'node', '-e', NODE_CHECK], input=json.dumps(credentials), text=True, capture_output=True)
                if check.returncode == 0:
                    restored = json.loads(check.stdout)
                    if restored != expected:
                        raise RuntimeError('Restored library or progress counts differ from production')
                    result = {'backupKey': backup['key'], 'login': True, 'libraries': len(restored['libraryIds']), 'progressCount': restored['progressCount'], 'network': 'none', 'productionModified': False}
                    (settings.data / 'restore-verification.json').write_text(json.dumps(result, indent=2))
                    print(json.dumps(result))
                    return
                time.sleep(1)
            raise RuntimeError('Isolated restored server did not become healthy within 60 checks')
        finally:
            subprocess.run(['docker', 'rm', '-f', name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
