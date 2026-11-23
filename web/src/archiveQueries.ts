import { useEffect, useSyncExternalStore } from 'react'
import type { ArchiveEntry } from './compositionArchiveStore'

export type ArchiveFolder = { day: string; count: number; folder?: string; batch?: string }
type Result = { items?: ArchiveEntry[]; folders?: ArchiveFolder[]; trash_count?: number; next_cursor?: string | null; total?: number; expires_at?: string | null }
type Snapshot = { data: Result; loaded: boolean; loading: boolean; error: string }
type Query = { snapshot: Snapshot; at: number; revision: number; pending: Promise<void> | null; listeners: Set<() => void>; timer?: ReturnType<typeof setTimeout> }
const empty: Snapshot = { data: {}, loaded: false, loading: false, error: '' }
const queries = new Map<string, Query>()

function getQuery(key: string) {
  let query = queries.get(key)
  if (!query) {
    query = { snapshot: empty, at: 0, revision: 0, pending: null, listeners: new Set() }
    queries.set(key, query)
    if (queries.size > 32) {
      const removable = [...queries].find(([name, value]) => name !== key && !value.listeners.size && !value.pending)
      if (removable) queries.delete(removable[0])
    }
  }
  return query
}

function publish(query: Query, patch: Partial<Snapshot>) {
  query.snapshot = { ...query.snapshot, ...patch }
  query.listeners.forEach((listener) => listener())
}

export function readArchiveQuery(key: string | null, force = false, more = false): Promise<void> {
  if (!key) return Promise.resolve()
  const query = getQuery(key)
  if (query.pending) return query.pending
  if (!force && !more && query.snapshot.loaded && Date.now() - query.at < 60_000) return Promise.resolve()
  const cursor = more ? query.snapshot.data.next_cursor : null
  if (more && !cursor) return Promise.resolve()
  const revision = query.revision
  publish(query, { loading: true, error: '' })
  const params = new URLSearchParams(key)
  if (cursor) params.set('cursor', cursor)
  query.pending = fetch(`/api/composition-archive?${params}`, { signal: AbortSignal.timeout(20_000) })
    .then(async (response) => {
      if (revision !== query.revision) return
      if (!response.ok) {
        if (response.status === 401) publish(query, empty)
        const payload = await response.json().catch(() => null)
        throw new Error(payload?.detail || '空间站读取失败，请重试')
      }
      const data = await response.json() as Result
      if (revision !== query.revision) return
      if (more) {
        data.items = [...new Map([...(query.snapshot.data.items || []), ...(data.items || [])].map((item) => [item.archive_key || item.id, item])).values()]
      }
      query.at = Date.now()
      publish(query, { data, loaded: true })
    })
    .catch((cause) => { if (revision === query.revision) publish(query, { error: cause instanceof Error ? cause.message : '空间站读取失败，请重试' }) })
    .finally(() => {
      query.pending = null
      publish(query, { loading: false })
      if (revision !== query.revision && query.listeners.size) void readArchiveQuery(key, true)
    })
  return query.pending
}

export function invalidateArchiveQueries() {
  queries.forEach((query, key) => {
    query.at = 0
    query.revision += 1
    if (query.listeners.size) void readArchiveQuery(key, true)
  })
}

export function clearArchiveQueries() {
  queries.forEach(query => publish(query, empty))
  invalidateArchiveQueries()
}

export function getArchiveQuerySnapshot(key: string) {
  return getQuery(key).snapshot
}

export function useArchiveQuery(key: string | null) {
  const snapshot = useSyncExternalStore(
    (listener) => {
      if (!key) return () => {}
      const query = getQuery(key)
      query.listeners.add(listener)
      // Revalidate visible directories periodically, including automatically expired files.
      query.timer ||= setInterval(() => { void readArchiveQuery(key) }, 60_000)
      return () => {
        query.listeners.delete(listener)
        if (!query.listeners.size) { clearInterval(query.timer); query.timer = undefined }
      }
    },
    () => key ? getArchiveQuerySnapshot(key) : empty,
    () => empty,
  )
  useEffect(() => { void readArchiveQuery(key) }, [key])
  return snapshot
}
