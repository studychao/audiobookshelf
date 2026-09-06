import { ImportClient } from './import-client.mjs'
import { fileKind, defaultEbook, suggestedTitle, extension } from './media-files.mjs'

const $ = (id) => document.getElementById(id)
let client, files = [], busy = false
const pendingChunks = new Map()
const backupDate = (value) => { const iso = value.replace(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})\d*Z$/, '$1-$2-$3T$4:$5:$6Z'); const date = new Date(iso); return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-CN') }
const bytes = (size) => size >= 1024 ** 3 ? `${(size / 1024 ** 3).toFixed(1)} GB` : size >= 1024 ** 2 ? `${(size / 1024 ** 2).toFixed(1)} MB` : `${(size / 1024).toFixed(1)} KB`
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; $('message').hidden = !text }
function setBusy(value) { busy = value; ['upload', 'reset', 'sort', 'files', 'folder', 'title', 'author', 'narrator', 'series', 'library', 'primary-ebook'].forEach((id) => { $(id).disabled = value }); $('pause').hidden = !value; renderFiles() }
async function authenticate(token) {
  if (client) client.token = token
  else client = new ImportClient(token, ({ loaded, total, name, status }) => {
    $('progress').value = loaded / total * 100
    $('progress-text').textContent = `${status} ${name} · ${bytes(loaded)} / ${bytes(total)}`
  })
  const { libraries } = await client.api('/libraries')
  $('library').replaceChildren(...libraries.map((library) => { const option = document.createElement('option'); option.value = library.id; option.textContent = library.name; return option }))
  if (!libraries.length) throw new Error('没有可用的书库，请先检查书库配置。')
  $('login').hidden = true; $('workspace').hidden = false; $('logout').hidden = false; message('')
}
$('logout').onclick = () => { if (busy) return message('请先暂停当前上传。', true); sessionStorage.removeItem('abs-personal-token'); client = null; $('workspace').hidden = true; $('login').hidden = false; $('logout').hidden = true; message('') }
$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault()
  const formElement = event.currentTarget
  const form = new FormData(formElement)
  try { const response = await fetch('/personal/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.fromEntries(form)) }); if (!response.ok) throw new Error('登录失败，请检查用户名和密码。'); const { token } = await response.json(); await authenticate(token); sessionStorage.setItem('abs-personal-token', token); formElement.reset() }
  catch (error) { message(error.message, true) }
})

function renderFiles() {
  const audioFiles = files.filter((file) => fileKind(file.name) === 'audio')
  $('list-title').textContent = audioFiles.length ? '章节与文件' : '电子书与封面'
  $('sort').hidden = audioFiles.length < 2
  $('count').textContent = `${files.length} 个文件 · ${bytes(files.reduce((sum, file) => sum + file.size, 0))}`
  $('chapters').replaceChildren(...files.map((file, index) => {
    const row = document.createElement('li')
    const number = document.createElement('span'); number.className = 'number'; number.textContent = fileKind(file.name) === 'audio' ? `${audioFiles.indexOf(file) + 1}` : fileKind(file.name) === 'ebook' ? '书' : '图'
    const name = document.createElement('span'); name.className = 'filename'; name.textContent = file.name
    const size = document.createElement('small'); size.textContent = bytes(file.size)
    row.append(number, name, size)
    if (fileKind(file.name) !== 'audio') { const type = document.createElement('small'); type.textContent = extension(file.name).toUpperCase(); row.append(type); return row }
    for (const [label, delta, symbol] of [['上移', -1, '↑'], ['下移', 1, '↓']]) { const button = document.createElement('button'); button.type = 'button'; button.textContent = symbol; button.setAttribute('aria-label', `${label} ${file.name}`); const target = files.indexOf(audioFiles[audioFiles.indexOf(file) + delta]); button.disabled = busy || target < 0; button.onclick = () => { [files[index], files[target]] = [files[target], files[index]]; renderFiles() }; row.append(button) }
    return row
  }))
}
async function analyze(sort = false) {
  const result = await client.api('/analyze', 'POST', files.map((file) => file.name))
  if (sort) files = result.order.map((index) => files[index])
  const notes = []
  if (result.missing.length) notes.push(`可能缺少章节：${result.missing.join('、')}，请核对。`)
  if (result.duplicateNumbers.length) notes.push(`重复章节编号：${result.duplicateNumbers.join('、')}，可能是上下集，请核对。`)
  if (result.duplicateNames.length) notes.push(`同名文件：${result.duplicateNames.join('、')}，请修改后重新选择。`)
  $('warnings').textContent = notes.join('\n'); $('warnings').hidden = !notes.length
  renderFiles()
}
async function choose(selected) {
  if (busy || !client) return
  files = Array.from(selected).filter((file) => fileKind(file.name))
  if (!files.length) return message('没有找到支持的音频、电子书或封面文件。', true)
  const topFolders = new Set(files.map((file) => (file.webkitRelativePath || '').split('/').slice(0, -1).join('/')).filter(Boolean))
  if (topFolders.size > 1) return message('请一次选择一本书的文件夹，多个文件夹请分别导入。', true)
  $('title').value = suggestedTitle(files)
  $('narrator-field').hidden = !files.some((file) => fileKind(file.name) === 'audio')
  const ebooks = files.filter((file) => fileKind(file.name) === 'ebook')
  $('primary-field').hidden = ebooks.length < 2
  $('primary-ebook').replaceChildren(...ebooks.map((file) => { const option = document.createElement('option'); option.value = file.name; option.textContent = file.name; return option }))
  $('primary-ebook').value = defaultEbook(files)
  $('import-form').hidden = false; $('drop').hidden = true; $('progress-box').hidden = true; $('upload').textContent = '上传到书库'; message('')
  try { await analyze(true) } catch (error) { message(error.message, true); renderFiles() }
}
for (const id of ['files', 'folder']) $(id).onchange = (event) => choose(event.target.files)
$('drop').ondragover = (event) => { event.preventDefault(); $('drop').classList.add('dragging') }
$('drop').ondragleave = () => $('drop').classList.remove('dragging')
$('drop').ondrop = (event) => { event.preventDefault(); $('drop').classList.remove('dragging'); choose(event.dataTransfer.files) }
$('sort').onclick = () => analyze(true).catch((error) => message(error.message, true))
$('reset').onclick = () => { files = []; $('files').value = ''; $('folder').value = ''; $('import-form').hidden = true; $('drop').hidden = false; message('') }
$('pause').onclick = () => { client.paused = true; $('pause').disabled = true }
$('import-form').onsubmit = async (event) => {
  event.preventDefault(); if (busy) return; setBusy(true); $('pause').disabled = false; $('progress-box').hidden = false; message('')
  try {
    await client.upload({ title: $('title').value.trim(), author: $('author').value.trim(), narrator: files.some((file) => fileKind(file.name) === 'audio') ? $('narrator').value.trim() : '', series: $('series').value.trim(), library_id: $('library').value, ...($('primary-ebook').value ? { primary_ebook: $('primary-ebook').value } : {}) }, files)
    message(files.some((file) => fileKind(file.name) === 'ebook') ? '已加入书库。返回书架，打开这本书即可阅读。' : '已加入书库，可以播放了。'); $('progress-text').textContent = '上传完成'; $('upload').textContent = '已完成（重复提交不会再次导入）'
    if (window.parent !== window) window.parent.postMessage({ type: 'abs-personal-complete', sharedIds: files.map((file) => file.sharedId).filter(Boolean) }, '*')
  } catch (error) { message(error.message, true) }
  finally { setBusy(false) }
}

async function showTab(name) {
  for (const tab of ['import', 'organize', 'backup']) $(`${tab}-panel`).hidden = tab !== name
  document.querySelectorAll('[data-tab]').forEach((button) => button.classList.toggle('selected', button.dataset.tab === name))
  message('')
  if (name === 'organize') {
    const { books } = await client.api('/books')
    $('book-list').replaceChildren(...books.map((book) => { const label = document.createElement('label'); label.className = 'book-row'; const check = document.createElement('input'); check.type = 'checkbox'; check.value = book.id; const title = document.createElement('span'); title.textContent = book.media.metadata.title; label.append(check, title); return label }))
    if (!books.length) $('book-list').textContent = '书库里还没有书，先添加第一本。'
  }
  if (name === 'backup') {
    const { last } = await client.api('/backups')
    $('backup-status').textContent = last ? `最近备份：${backupDate(last.createdAt)}\n${bytes(last.size)}，文件和数据库完整性校验通过。` : '还没有备份。可以现在创建第一份。'
  }
}
document.querySelectorAll('[data-tab]').forEach((button) => { button.onclick = () => showTab(button.dataset.tab).catch((error) => message(error.message, true)) })
$('metadata-form').onsubmit = async (event) => {
  event.preventDefault()
  const ids = Array.from(document.querySelectorAll('#book-list input:checked'), (input) => input.value)
  if (!ids.length) return message('请先选择书籍。', true)
  const changes = { ids }
  for (const key of ['author', 'narrator', 'series']) if ($(`edit-${key}`).checked) changes[key] = $(`batch-${key}`).value.trim()
  if (Object.keys(changes).length === 1) return message('请勾选需要更新的字段。', true)
  try { const { updated } = await client.api('/books', 'PATCH', changes); message(`已更新 ${updated.length} 本书。`) } catch (error) { message(error.message, true) }
}
$('backup-now').onclick = async () => { $('backup-now').disabled = true; message('正在创建并校验备份…'); try { await client.api('/backups', 'POST'); await showTab('backup'); message('备份已保存到 OSS，完整性校验通过。') } catch (error) { message(error.message, true) } finally { $('backup-now').disabled = false } }

function sharedFile(descriptor) {
  return { ...descriptor, sharedId: descriptor.id, slice(start, end) { return { size: end - start, async arrayBuffer() { const requestId = crypto.randomUUID(); return new Promise((resolve, reject) => { const timer = setTimeout(() => { pendingChunks.delete(requestId); reject(new Error('读取共享文件超时')) }, 30000); pendingChunks.set(requestId, { resolve, reject, timer }); window.parent.postMessage({ type: 'abs-personal-read', requestId, id: descriptor.id, start, length: end - start }, '*') }) } } } }
}
window.addEventListener('message', async (event) => {
  if (event.source !== window.parent || !['capacitor://localhost', 'http://localhost'].includes(event.origin)) return
  if (event.data.type === 'abs-personal-auth') {
    try { await authenticate(event.data.token); if (event.data.files?.length) await choose(event.data.files.map(sharedFile)) } catch (error) { message(error.message, true) }
  }
  if (event.data.type === 'abs-personal-chunk') {
    const pending = pendingChunks.get(event.data.requestId); if (!pending) return
    clearTimeout(pending.timer); pendingChunks.delete(event.data.requestId)
    if (event.data.error) pending.reject(new Error(event.data.error))
    else pending.resolve(Uint8Array.from(atob(event.data.data), (c) => c.charCodeAt(0)).buffer)
  }
})
if (window.parent !== window) { document.querySelector('header').hidden = true; window.parent.postMessage({ type: 'abs-personal-ready' }, '*') }
else if (sessionStorage.getItem('abs-personal-token') || localStorage.getItem('token')) authenticate(sessionStorage.getItem('abs-personal-token') || localStorage.getItem('token')).catch(() => { sessionStorage.removeItem('abs-personal-token'); message('请重新登录。') })
