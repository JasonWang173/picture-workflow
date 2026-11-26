import type { ArchiveFolder } from './archiveQueries'

export type Directory = { folders: ArchiveFolder[]; expires_at?: string | null }
const prefix = 'composition-archive:directory:v1:'

// Keep the remaining folder counts searchable even when only part of a batch has expired.
export function currentDirectory(data: Directory, now = Date.now()): Directory | undefined {
  if (!data.expires_at || Date.parse(data.expires_at) > now) return data
  const folders: ArchiveFolder[] = []
  let nextExpiry = Infinity
  for (const folder of data.folders) {
    const offsets = folder.expiry_offsets_ms
    if (!offsets?.length || offsets.length !== folder.count) return undefined // Pre-upgrade directory.
    let expired = 0
    let expiry = offsets[0]
    while (expiry <= now) {
      expired++
      if (expired === offsets.length) break
      expiry += offsets[expired]
    }
    if (expired === offsets.length) continue
    nextExpiry = Math.min(nextExpiry, expiry)
    folders.push(expired ? { ...folder, count: offsets.length - expired, expiry_offsets_ms: [expiry, ...offsets.slice(expired + 1)] } : folder)
  }
  return { folders, expires_at: Number.isFinite(nextExpiry) ? new Date(nextExpiry).toISOString() : null }
}

function directoryKey(params: URLSearchParams) {
  const section = params.get('section')
  if (params.get('view') === 'batch_folders' && params.get('scope') === 'team' && params.get('viewer')
      && (section === 'history' || section === 'trash')) return prefix + section
}

export function readDirectory(params: URLSearchParams): { data: Directory; checkedAt: number } | undefined {
  const key = directoryKey(params)
  if (!key) return
  try {
    const saved = JSON.parse(localStorage.getItem(key) || 'null')
    if (saved?.viewer === params.get('viewer') && Array.isArray(saved.data?.folders)
        && (saved.until === undefined || saved.until > Date.now())) {
      const data = currentDirectory(saved.data)
      if (data) {
        if (data !== saved.data) {
          try { localStorage.setItem(key, JSON.stringify({ ...saved, data })) } catch { /* Still usable in memory. */ }
        }
        return { data, checkedAt: Number(saved.checkedAt) || 0 }
      }
    }
  } catch { /* Browser storage is optional. */ }
}

export function saveDirectory(params: URLSearchParams, data: Directory) {
  const key = directoryKey(params)
  if (!key) return
  try {
    localStorage.setItem(key, JSON.stringify({ viewer: params.get('viewer'), checkedAt: Date.now(), data }))
  } catch { /* The in-memory directory still works if storage is full or disabled. */ }
}

export function forgetDirectories() {
  try {
    localStorage.removeItem(prefix + 'history')
    localStorage.removeItem(prefix + 'trash')
  } catch { /* Storage may be disabled. */ }
}
