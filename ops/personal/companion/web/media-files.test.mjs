import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fileKind, defaultEbook, suggestedTitle } from './media-files.mjs'

test('recognizes supported ebook formats without admitting arbitrary files', () => {
  for (const name of ['书.EPUB', '书.pdf', '书.mobi', '书.azw3', '书.cbz', '书.cbr']) assert.equal(fileKind(name), 'ebook')
  assert.equal(fileKind('封面.jpg'), 'cover')
  assert.equal(fileKind('第一章.mp3'), 'audio')
  assert.equal(fileKind('恶意.exe'), null)
})
test('prefers EPUB for reading and preserves numbered ebook titles', () => {
  const files = [{ name: '封面.jpg' }, { name: '第十二夜.pdf' }, { name: '第十二夜.epub' }]
  assert.equal(defaultEbook(files), '第十二夜.epub')
  assert.equal(suggestedTitle(files), '第十二夜')
  assert.equal(suggestedTitle([{ name: '1984.epub' }]), '1984')
  assert.equal(defaultEbook([{ name: '第一集.mp3' }]), '')
})
