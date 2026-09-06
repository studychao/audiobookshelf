"""Verify generated EPUB/PDF imports and mixed books; remove all test media afterward."""
from __future__ import annotations

import hashlib
from html import escape
import io
import json
from pathlib import Path
import wave
from uuid import uuid4
import zipfile

import httpx
import oss2

from service import connect


def epub(title: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as book:
        book.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        book.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        book.writestr('OEBPS/content.opf', f'''<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">urn:uuid:{uuid4()}</dc:identifier><dc:title>{escape(title)}</dc:title><dc:creator>Verification</dc:creator><dc:language>zh-CN</dc:language></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="ncx"><itemref idref="chapter"/></spine></package>''')
        book.writestr('OEBPS/chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>电子书验证</title></head><body><h1>电子书上传验证</h1><p>这是一段自动生成的测试正文，用于检查阅读与进度保存。</p></body></html>')
        book.writestr('OEBPS/toc.ncx', '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/><docTitle><text>Verification</text></docTitle><navMap><navPoint id="chapter" playOrder="1"><navLabel><text>第一章</text></navLabel><content src="chapter.xhtml"/></navPoint></navMap></ncx>')
    return output.getvalue()


def pdf() -> bytes:
    stream = b'BT /F1 18 Tf 72 720 Td (Ebook upload verification) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>', b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    output = bytearray(b'%PDF-1.4\n'); offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output)); output.extend(f'{index} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref = len(output)
    output.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets:
        output.extend(f'{offset:010} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


def audio() -> bytes:
    output = io.BytesIO()
    with wave.open(output, 'wb') as file:
        file.setnchannels(1); file.setsampwidth(2); file.setframerate(8000); file.writeframes(b'\0\0'*16000)
    return output.getvalue()


def main() -> None:
    origin = 'https://audiobook.47-77-238-23.sslip.io'
    credentials = json.loads(Path('/opt/audiobookshelf/secrets/admin.json').read_text())
    with httpx.Client(base_url=origin, timeout=180) as client:
        login = client.post('/personal/api/login', json=credentials); login.raise_for_status()
        client.headers['Authorization'] = 'Bearer '+login.json()['token']
        def api(method: str, path: str, **kwargs: object) -> httpx.Response:
            result = client.request(method, '/personal/api'+path, **kwargs); result.raise_for_status(); return result
        library = api('GET', '/libraries').json()['libraries'][0]
        reports = []
        for mode in ['epub', 'pdf', 'mixed']:
            title = f'Ebook verification {mode} {uuid4().hex[:10]}'
            payloads = {'正文.epub': epub(title)} if mode == 'epub' else {'正文.pdf': pdf()} if mode == 'pdf' else {'第1集.wav': audio(), '正文.epub': epub(title), '排版.pdf': pdf()}
            selected = '排版.pdf' if mode == 'mixed' else next(iter(payloads))
            spec = {'title': title, 'library_id': library['id'], 'primary_ebook': selected, 'files': [{'name': name, 'size': len(data), 'fingerprint': hashlib.sha256(data).hexdigest()} for name, data in payloads.items()]}
            job = api('POST', '/imports', json=spec).json()
            book_id = None
            try:
                for file in job['files']:
                    base = f"/imports/{job['id']}/files/{file['id']}"
                    data = payloads[file['name']]
                    for number, offset in enumerate(range(0, len(data), job['partSize']), 1):
                        url = api('POST', base+f'/parts/{number}/url').json()['url']
                        response = httpx.put(url, content=data[offset:offset+job['partSize']], timeout=120)
                        if response.status_code != 200:
                            raise RuntimeError(f'Signed upload returned HTTP {response.status_code}')
                    api('POST', base+'/complete')
                imported = api('POST', f"/imports/{job['id']}/complete").json()
                book_id = imported['bookId']
                response = client.get(f'/api/items/{book_id}?expanded=1'); response.raise_for_status(); item = response.json()
                assert item['media']['ebookFile']['metadata']['filename'] == selected
                assert bool(item['media']['audioFiles']) == (mode == 'mixed')
                read = client.get(f'/api/items/{book_id}/ebook'); read.raise_for_status()
                assert hashlib.sha256(read.content).digest() == hashlib.sha256(payloads[selected]).digest()
                for file in job['files']:
                    stored = connect().get_object(job['destination']+'/'+file['destination']).read()
                    assert hashlib.sha256(stored).digest() == hashlib.sha256(payloads[file['name']]).digest()
                location = 'epubcfi(/6/2[chapter]!/4/2/1:0)' if mode == 'epub' else '1'
                progress = client.patch(f'/api/me/progress/{book_id}', json={'ebookLocation': location, 'ebookProgress': 0.25}); progress.raise_for_status()
                saved = client.get(f'/api/me/progress/{book_id}').json()
                assert saved['ebookLocation'] == location and saved['ebookProgress'] == 0.25
                reports.append({'mode': mode, 'primary': selected, 'readable': True, 'oss_sha256': True, 'reading_progress': True})
                print(f'{mode}: signed upload, catalog, reader bytes, OSS hash and reading progress passed', flush=True)
            finally:
                if not book_id:
                    relative = job['destination'].removeprefix('audiobookshelf/audiobooks/')
                    match = next((book for book in api('GET', '/books').json()['books'] if book.get('relPath') == relative), None)
                    book_id = match['id'] if match else None
                if book_id:
                    deleted = client.delete(f'/api/items/{book_id}?hard=1'); deleted.raise_for_status()
                bucket = connect()
                for prefix in [job['destination']+'/', f"audiobookshelf/.imports/{job['id']}/"]:
                    for obj in list(oss2.ObjectIterator(bucket, prefix=prefix)):
                        bucket.delete_object(obj.key)
        Path('/opt/audiobookshelf/personal-data/ebook-verification.json').write_text(json.dumps({'cases': reports, 'test_books_removed': True}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
