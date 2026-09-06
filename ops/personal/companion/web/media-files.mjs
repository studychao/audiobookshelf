const audio = new Set(['mp3', 'm4b', 'm4a', 'aac', 'ogg', 'opus', 'flac', 'wav'])
const ebooks = new Set(['epub', 'pdf', 'mobi', 'azw3', 'cbz', 'cbr'])
const covers = new Set(['jpg', 'jpeg', 'png', 'webp'])
export const extension = (name) => name.split('.').at(-1).toLowerCase()
export function fileKind(name) {
  const ext = extension(name)
  return audio.has(ext) ? 'audio' : ebooks.has(ext) ? 'ebook' : covers.has(ext) ? 'cover' : null
}
export function defaultEbook(files) {
  return files.find((file) => extension(file.name) === 'epub')?.name || files.find((file) => fileKind(file.name) === 'ebook')?.name || ''
}
export function suggestedTitle(files) {
  const file = files.find((file) => fileKind(file.name) === 'ebook') || files.find((file) => fileKind(file.name) === 'audio')
  if (!file) return ''
  const name = file.name.replace(/\.[^.]+$/, '')
  return file.webkitRelativePath?.split('/').slice(-2, -1)[0] || (fileKind(file.name) === 'audio' ? name.replace(/[ _-]*第?[\d零〇一二三四五六七八九十百千万两]+[章集回节].*$/, '') : name)
}
