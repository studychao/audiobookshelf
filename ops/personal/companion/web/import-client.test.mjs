import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ImportClient, fingerprint } from './import-client.mjs'

test('file identity changes when content or size changes', async () => {
  const first = new File(['abcd'], '第一集.mp3'), changed = new File(['abce'], '第一集.mp3')
  assert.notEqual(await fingerprint(first), await fingerprint(changed))
  assert.equal(await fingerprint(first), await fingerprint(new File(['abcd'], '第一集.mp3')))
})

test('resume skips stored parts and completes only after every file', async (t) => {
  const file = new File(['abcdefgh'], '第一集.mp3')
  const calls = []
  const job = { id: 'job', state: 'uploading', partSize: 4, files: [{ id: 'file', complete: false, parts: [{ number: 1, size: 4 }] }] }
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    calls.push({ url, options })
    if (url.endsWith('/url')) return Response.json({ url: 'https://storage.invalid/part' })
    if (url === 'https://storage.invalid/part') return new Response('', { status: 200 })
    if (url.endsWith('/imports/job/complete')) return Response.json({ state: 'complete' })
    return Response.json(job)
  })
  const result = await new ImportClient('test').upload({ title: '测试书', library_id: 'books' }, [file])
  assert.equal(result.state, 'complete')
  const upload = calls.find((call) => call.url === 'https://storage.invalid/part')
  assert.equal(await upload.options.body.text(), 'efgh')
  assert.equal(calls.filter((call) => call.url.includes('/parts/1')).length, 0)
  assert.equal(calls.at(-1).url, '/personal/api/imports/job/complete')
})

test('CORS failure falls back to a bounded part upload with auth', async (t) => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    calls.push({ url, options })
    if (url.endsWith('/url')) return Response.json({ url: 'https://storage.invalid/part' })
    if (url.startsWith('https:')) throw new TypeError('CORS blocked')
    return Response.json({ ok: true })
  })
  const client = new ImportClient('test')
  await client.putPart('/imports/job/files/file/parts/1', new Blob(['part']))
  assert.equal(calls.at(-1).options.headers.Authorization, 'Bearer test')
  assert.equal(await calls.at(-1).options.body.text(), 'part')
  assert.equal(client.useProxy, true)
})
