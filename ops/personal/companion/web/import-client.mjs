export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

export async function fingerprint(file) {
  const head = new Uint8Array(await file.slice(0, 65536).arrayBuffer())
  const tail = new Uint8Array(await file.slice(Math.max(0, file.size - 65536), file.size).arrayBuffer())
  const identity = new TextEncoder().encode(`${file.name}\n${file.size}\n`)
  const data = new Uint8Array(head.length + tail.length + identity.length)
  data.set(identity); data.set(head, identity.length); data.set(tail, identity.length + head.length)
  const digest = await crypto.subtle.digest('SHA-256', data)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export class ImportClient {
  constructor(token, onProgress = () => {}) {
    this.token = token
    this.onProgress = onProgress
    this.paused = false
    this.useProxy = false
  }

  async api(path, method = 'GET', body) {
    const response = await fetch(`/personal/api${path}`, {
      method, headers: { Authorization: `Bearer ${this.token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      body: body === undefined ? undefined : JSON.stringify(body)
    })
    if (!response.ok) {
      const error = await response.json().catch(() => ({}))
      const message = typeof error.detail === 'string' ? error.detail : `请求失败 (${response.status})`
      const failure = new Error(message)
      failure.status = response.status
      throw failure
    }
    return response.json()
  }

  async putPart(path, chunk) {
    if (!(chunk instanceof Blob)) chunk = new Blob([await chunk.arrayBuffer()])
    if (!this.useProxy) {
      const { url } = await this.api(`${path}/url`, 'POST')
      try {
        const response = await fetch(url, { method: 'PUT', body: chunk })
        if (!response.ok) throw new Error(`上传返回 ${response.status}`)
        return
      } catch (error) {
        // CORS failures and network transitions can retry the same numbered part safely.
        this.useProxy = true
      }
    }
    const response = await fetch(`/personal/api${path}`, { method: 'PUT', headers: { Authorization: `Bearer ${this.token}` }, body: chunk })
    if (!response.ok) {
      const failure = new Error(`分片上传失败 (${response.status})`)
      failure.status = response.status
      throw failure
    }
  }

  async upload(metadata, files) {
    this.paused = false
    const descriptions = []
    for (const file of files) descriptions.push({ name: file.name, size: file.size, fingerprint: await fingerprint(file) })
    let job = await this.api('/imports', 'POST', { ...metadata, files: descriptions })
    if (job.state === 'complete') return job
    job = await this.api(`/imports/${job.id}`)
    const total = files.reduce((sum, file) => sum + file.size, 0)
    let loaded = 0
    for (let index = 0; index < files.length; index++) {
      const source = files[index], target = job.files[index]
      if (target.complete) { loaded += source.size; continue }
      const existing = new Map(target.parts.map((part) => [part.number, part.size]))
      for (let offset = 0, number = 1; offset < source.size; offset += job.partSize, number++) {
        if (this.paused) throw new Error('已暂停。重新开始会继续已完成的分片。')
        const chunk = source.slice(offset, Math.min(source.size, offset + job.partSize))
        if (existing.get(number) !== chunk.size) {
          for (let attempt = 0; ; attempt++) {
            try { await this.putPart(`/imports/${job.id}/files/${target.id}/parts/${number}`, chunk); break }
            catch (error) {
              if ([401, 403, 409, 413, 422].includes(error.status) || attempt === 3 || this.paused) throw error
              this.onProgress({ loaded, total, name: source.name, status: `连接中断，${2 ** attempt} 秒后重试` })
              await sleep(1000 * 2 ** attempt)
            }
          }
        }
        loaded += chunk.size
        this.onProgress({ loaded, total, name: source.name, status: '上传中' })
      }
      await this.api(`/imports/${job.id}/files/${target.id}/complete`, 'POST')
    }
    this.onProgress({ loaded: total, total, name: '', status: '文件已上传，正在校验并加入书库…' })
    return this.api(`/imports/${job.id}/complete`, 'POST')
  }
}
