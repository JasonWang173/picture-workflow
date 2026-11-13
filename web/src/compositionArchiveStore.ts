import { useSyncExternalStore } from 'react'

export type ArchiveEntry = {
  id: string
  name: string
  template_name: string
  product_name: string
  section: 'history' | 'trash'
  saved_at: string
  expires_at: string
}

export const archiveEndpoint = '/api/composition-archive'
const cacheLifetime = 60_000
let snapshot = { items: [] as ArchiveEntry[], loaded: false, loading: false, error: '' }
let lastRead = 0
let pending: Promise<void> | null = null
const updatesDuringRead = new Map<string, ArchiveEntry>()
const listeners = new Set<() => void>()
let expiryTimer: ReturnType<typeof setTimeout> | undefined

function currentEntries(items: Iterable<ArchiveEntry>) {
  const now = Date.now()
  return [...items].filter((item) => Date.parse(item.expires_at) > now)
    .sort((a, b) => Date.parse(b.saved_at) - Date.parse(a.saved_at))
}

function publish(next: Partial<typeof snapshot>) {
  snapshot = { ...snapshot, ...next }
  clearTimeout(expiryTimer)
  if (listeners.size && snapshot.items.length) {
    const nextExpiry = snapshot.items.reduce((soonest, item) => Math.min(soonest, Date.parse(item.expires_at)), Infinity)
    expiryTimer = setTimeout(() => publish({ items: currentEntries(snapshot.items) }), Math.max(0, nextExpiry - Date.now()))
  }
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (!listeners.size) clearTimeout(expiryTimer)
  }
}

export function useCompositionArchive() {
  return useSyncExternalStore(subscribe, getArchiveSnapshot, getArchiveSnapshot)
}

export function getArchiveSnapshot() {
  return snapshot
}

export async function checkArchiveResponse(response: Response, fallback: string) {
  if (response.ok) return
  const payload = await response.json().catch(() => null)
  throw new Error(typeof payload?.detail === 'string' ? payload.detail : fallback)
}

export function updateArchiveRecords(items: ArchiveEntry[]) {
  const merged = new Map(snapshot.items.map((item) => [item.id, item]))
  for (const item of items) {
    merged.set(item.id, item)
    if (pending) updatesDuringRead.set(item.id, item)
  }
  publish({ items: currentEntries(merged.values()) })
}

export function readCompositionArchive(force = false) {
  publish({ items: currentEntries(snapshot.items) })
  if (pending) return pending
  if (!force && snapshot.loaded && Date.now() - lastRead < cacheLifetime) return Promise.resolve()

  updatesDuringRead.clear()
  publish({ loading: true, error: '' })
  pending = fetch(archiveEndpoint)
    .then(async (response) => {
      await checkArchiveResponse(response, '空间站读取失败，请重试')
      const { items } = await response.json() as { items: ArchiveEntry[] }
      const merged = new Map(items.map((item) => [item.id, item]))
      updatesDuringRead.forEach((item, id) => merged.set(id, item))
      lastRead = Date.now()
      publish({ items: currentEntries(merged.values()), loaded: true })
    })
    .catch((cause) => publish({ error: cause instanceof Error ? cause.message : '空间站读取失败，请重试' }))
    .finally(() => {
      pending = null
      updatesDuringRead.clear()
      publish({ loading: false })
    })
  return pending
}
