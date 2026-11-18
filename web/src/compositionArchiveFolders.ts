import type { ArchiveEntry } from './compositionArchiveStore'

export type ArchiveDayGroup = {
  key: string
  label: string
  weekday: string
  items: ArchiveEntry[]
}

const archiveTimeZone = 'Asia/Shanghai'
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
