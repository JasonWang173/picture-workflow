import { useEffect, useSyncExternalStore } from 'react'
import type { ArchiveEntry } from './compositionArchiveStore'
import { currentDirectory, forgetDirectories, readDirectory, saveDirectory } from './archiveDirectoryCache'

export type ArchiveFolder = { day: string; count: number; folder?: string; batch?: string; latest_at?: string; username?: string; expiry_offsets_ms?: number[] }
type Result = { items?: ArchiveEntry[]; folders?: ArchiveFolder[]; trash_count?: number; next_cursor?: string | null; total?: number; expires_at?: string | null }
type Snapshot = { data: Result; loaded: boolean; loading: boolean; error: string; syncing?: boolean }
type Query = { snapshot: Snapshot; at: number; revision: number; pending: Promise<void> | null; listeners: Set<() => void>; timer?: ReturnType<typeof setTimeout>; retry?: ReturnType<typeof setTimeout>; expiryTimer?: ReturnType<typeof setTimeout> }
const empty: Snapshot = { data: {}, loaded: false, loading: false, error: '' }
const queries = new Map<string, Query>()
const refreshInterval = 60_000
let warmKey: string | null = null

export function folderQueryKey(section: 'history' | 'trash', username?: string) {
  return `view=batch_folders&section=${section}&scope=${username ? 'team&viewer=' + encodeURIComponent(username) : 'browser'}`
}

export function warmArchiveHistory(username: string) {
  warmKey = folderQueryKey('history', username)
  return readArchiveQuery(warmKey)
}

function getQuery(key: string) {
  let query = queries.get(key)
  if (!query) {
    const cached = readDirectory(new URLSearchParams(key))
    query = { snapshot: cached ? { ...empty, data: cached.data, loaded: true } : empty,
      at: cached?.checkedAt ?? 0, revision: 0, pending: null, listeners: new Set() }
    queries.set(key, query)
    if (queries.size > 32) {
      const removable = [...queries].find(([name, value]) => name !== key && name !== warmKey && !value.listeners.size && !value.pending)
      if (removable) queries.delete(removable[0])
    }
  }
  return query
}

function publish(query: Query, patch: Partial<Snapshot>) {
  query.snapshot = { ...query.snapshot, ...patch }
  if (patch.data) scheduleExpiry(query)
  query.listeners.forEach((listener) => listener())
}

function pruneExpired(query: Query) {
  const { data } = query.snapshot
  if (!data.folders || !data.expires_at || Date.parse(data.expires_at) > Date.now()) return
  const remaining = currentDirectory({ folders: data.folders, expires_at: data.expires_at })
  if (remaining) publish(query, { data: { ...data, ...remaining } })
  else publish(query, { ...empty, syncing: false })
}

function scheduleExpiry(query: Query) {
  clearTimeout(query.expiryTimer)
  const expiry = query.snapshot.data.expires_at
  if (expiry && query.listeners.size) {
    query.expiryTimer = setTimeout(() => pruneExpired(query), Math.max(0, Date.parse(expiry) - Date.now()))
  }
}

export function readArchiveQuery(key: string | null, force: boolean | 'refresh' = false, more = false): Promise<void> {
  if (!key) return Promise.resolve()
  const query = getQuery(key)
  pruneExpired(query)
  if (query.pending) return query.pending
  if (!force && !more && query.snapshot.loaded && Date.now() - query.at < refreshInterval) return Promise.resolve()
  const cursor = more ? query.snapshot.data.next_cursor : null
  if (more && !cursor) return Promise.resolve()
  const revision = query.revision
  const params = new URLSearchParams(key)
  clearTimeout(query.retry)
  publish(query, { loading: true, error: '' })
  const requestParams = new URLSearchParams(params)
  requestParams.delete('viewer') // A confirmed account partitions browser storage; the server authenticates via cookie.
  if (force === 'refresh') requestParams.set('refresh', 'true')
  if (cursor) requestParams.set('cursor', cursor)
  query.pending = fetch(`/api/composition-archive?${requestParams}`, { signal: AbortSignal.timeout(20_000) })
    .then(async (response) => {
      if (revision !== query.revision) return
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          const payload = await response.json().catch(() => null)
          if (revision === query.revision) {
            clearArchiveQueries()
            publish(getQuery(key), { error: payload?.detail || '请重新登录后查看共享记录' })
          }
          return
        }
        const retryAfter = Number(response.headers.get('Retry-After'))
        if (response.status === 503 && retryAfter > 0 && (query.listeners.size || key === warmKey)) {
          publish(query, { syncing: true })
          query.retry = setTimeout(() => {
            if (revision === query.revision && (query.listeners.size || key === warmKey)) void readArchiveQuery(key, true, more)
          }, Math.min(30, Math.max(1, retryAfter)) * 1000)
          return
        }
        const payload = await response.json().catch(() => null)
        throw new Error(payload?.detail || '空间站读取失败，请重试')
      }
      const data = await response.json() as Result
      if (revision !== query.revision) return
      if (more) {
        data.items = [...new Map([...(query.snapshot.data.items || []), ...(data.items || [])].map((item) => [item.archive_key || item.id, item])).values()]
      }
      query.at = Date.now()
      if (data.folders) {
        const directory = currentDirectory({ folders: data.folders, expires_at: data.expires_at })
        if (directory) {
          Object.assign(data, directory)
          saveDirectory(params, directory)
        }
      }
      publish(query, { data, loaded: true, syncing: false })
      pruneExpired(query)
    })
    .catch((cause) => { if (revision === query.revision) publish(query, { syncing: false, error: cause instanceof Error ? cause.message : '空间站读取失败，请重试' }) })
    .finally(() => {
      query.pending = null
      publish(query, { loading: false })
      if (revision !== query.revision && queries.get(key) === query && (query.listeners.size || key === warmKey)) void readArchiveQuery(key, true)
    })
  return query.pending
}

export function invalidateArchiveQueries() {
  queries.forEach((query, key) => {
    query.at = 0
    query.revision += 1
    if (query.listeners.size || key === warmKey) void readArchiveQuery(key, true)
  })
}

export function clearArchiveQueries(forgetSaved = true) {
  warmKey = null
  if (forgetSaved) forgetDirectories()
  const old = [...queries.values()]
  queries.clear()
  for (const query of old) {
    query.revision++
    clearInterval(query.timer)
    clearTimeout(query.retry)
    publish(query, { ...empty, syncing: false })
  }
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
      scheduleExpiry(query)
      // Revalidate visible directories periodically, including automatically expired files.
      query.timer ||= setInterval(() => { void readArchiveQuery(key) }, refreshInterval)
      return () => {
        query.listeners.delete(listener)
        if (!query.listeners.size) { clearInterval(query.timer); query.timer = undefined; clearTimeout(query.expiryTimer) }
      }
    },
    () => key ? getArchiveQuerySnapshot(key) : empty,
    () => empty,
  )
  useEffect(() => { void readArchiveQuery(key) }, [key])
  return snapshot
}
