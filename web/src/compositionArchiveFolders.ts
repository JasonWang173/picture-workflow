import type { ArchiveEntry } from './compositionArchiveStore'
import type { ArchiveFolder } from './archiveQueries'

export function archiveBatchLabel(folder: ArchiveFolder) {
  const sequence = folder.folder?.match(/（(\d+)）$/)?.[1]
  return sequence ? `第 ${sequence} 批` : '历史记录'
}

export function groupArchiveFolders(folders: ArchiveFolder[]) {
  const accounts = new Map<string, { key: string; label: string; count: number; batches: number; days: Map<string, { day: string; count: number; folders: ArchiveFolder[] }> }>()
  for (const folder of folders) {
    if (folder.count <= 0) continue
    // Older caches and recycle-bin directories may only carry the account in the folder name.
    const username = (folder.username || folder.folder?.match(/^\d{4}-\d{2}-\d{2}-([a-z]+)（/i)?.[1] || '').toLowerCase()
    let account = accounts.get(username)
    if (!account) {
      account = { key: username, label: username || '未标记账号', count: 0, batches: 0, days: new Map() }
      accounts.set(username, account)
    }
    const day = account.days.get(folder.day) || { day: folder.day, count: 0, folders: [] }
    day.count += folder.count
    day.folders.push(folder)
    account.days.set(folder.day, day)
    account.count += folder.count
    account.batches++
  }
  return [...accounts.values()]
    .sort((a, b) => {
      if (!a.key) return 1
      if (!b.key) return -1
      return a.key.localeCompare(b.key, 'en')
    })
    .map(account => ({
      ...account,
      days: [...account.days.values()].sort((a, b) => b.day.localeCompare(a.day)).map(day => ({
        ...day,
        folders: day.folders.sort((a, b) =>
          (a.folder || a.day).localeCompare(b.folder || b.day, 'zh-CN', { numeric: true })
          || (a.batch || '').localeCompare(b.batch || '')),
      })),
    }))
}

export type ArchiveDayGroup = {
  key: string
  label: string
  weekday: string
  items: ArchiveEntry[]
}

const archiveTimeZone = 'Asia/Shanghai'
export const historyTimeFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: archiveTimeZone, month: 'numeric', day: 'numeric',
  hourCycle: 'h23', hour: '2-digit', minute: '2-digit', second: '2-digit',
})
const dayParts = new Intl.DateTimeFormat('zh-CN-u-ca-gregory', {
  timeZone: archiveTimeZone,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  weekday: 'short',
})

export function dayIdentity(value: string) {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return { key: 'unknown', label: '时间未知', weekday: '' }
  const parts = new Map(dayParts.formatToParts(new Date(timestamp)).map((part) => [part.type, part.value]))
  const year = parts.get('year') || '0000'
  const month = parts.get('month') || '00'
  const day = parts.get('day') || '00'
  return {
    key: `${year}-${month}-${day}`,
    label: `${year}年${Number(month)}月${Number(day)}日`,
    weekday: parts.get('weekday') || '',
  }
}

export function groupArchiveEntriesByDay(entries: ArchiveEntry[]): ArchiveDayGroup[] {
  const groups = new Map<string, ArchiveDayGroup>()
  for (const entry of entries) {
    const identity = dayIdentity(entry.downloaded_at || entry.saved_at)
    const group = groups.get(identity.key) || { ...identity, items: [] }
    group.items.push(entry)
    groups.set(identity.key, group)
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      items: group.items.slice().sort((a, b) => Date.parse(b.downloaded_at || b.saved_at) - Date.parse(a.downloaded_at || a.saved_at)),
    }))
    .sort((a, b) => {
      if (a.key === 'unknown') return 1
      if (b.key === 'unknown') return -1
      return b.key.localeCompare(a.key)
    })
}
