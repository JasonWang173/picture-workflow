import { useMemo, useState } from 'react'
import type { ArchiveFolder } from './archiveQueries'
import { archiveBatchLabel, dayIdentity, groupArchiveFolders, historyTimeFormat } from './compositionArchiveFolders'

export function ArchiveDirectory({ folders, portfolio, day, onSelect, onDay, onOpen, showEmpty }: {
  folders: ArchiveFolder[]
  portfolio: string | null
  day: string | null
  onSelect: (username: string) => void
  onDay: (day: string) => void
  onOpen: (folder: ArchiveFolder) => void
  showEmpty: boolean
}) {
  const [search, setSearch] = useState('')
  const query = search.trim().toLowerCase()
  const accounts = useMemo(() => groupArchiveFolders(folders), [folders])
  const account = accounts.find(item => item.key === portfolio)
  const date = account?.days.find(item => item.day === day)
  const results = accounts.filter(item => !query || item.label.toLowerCase().includes(query))
  const enter = (username: string) => { setSearch(''); onSelect(username) }

  if (portfolio === null) return <>
    <form className="archive-account-search" role="search" aria-label="搜索账号文件夹" onSubmit={event => {
      event.preventDefault()
      if (!query) return
      const match = results.find(item => item.key === query) || (results.length === 1 ? results[0] : undefined)
      if (match) enter(match.key)
    }}>
      <label htmlFor="archive-account-search">按账号查找作品集</label>
      <input id="archive-account-search" type="search" placeholder="搜索账号，例如 zs" autoComplete="off"
        autoCapitalize="none" spellCheck={false} enterKeyHint="search" value={search} onChange={event => setSearch(event.target.value)} />
    </form>
    {!!accounts.length && <div className="archive-folder-summary"><div>
      <strong>账号文件夹</strong>
      <span>{results.length} 个账号文件夹 · 共 {results.reduce((total, item) => total + item.count, 0)} 张素材</span>
    </div></div>}
    <div className="archive-folder-grid">
      {results.map(item => <button className="archive-folder-card" type="button" key={item.key} onClick={() => enter(item.key)} aria-label={`打开账号文件夹 ${item.label}`}>
        <span className="archive-folder-icon" aria-hidden="true" />
        <span className="archive-folder-meta"><strong>{item.label}</strong>
          <small>{item.days.length} 个日期 · {item.batches} 个批次</small><small>{item.count} 张素材</small></span>
        <span className="archive-folder-arrow" aria-hidden="true">→</span>
      </button>)}
    </div>
    {!results.length && showEmpty && <p className="archive-empty" role="status">{query ? `没有找到与「${search.trim()}」匹配的账号。` : '暂无记录，素材入库后会显示对应的账号文件夹。'}</p>}
  </>

  if (day === null) return <>
    <div className="archive-folder-summary"><div><strong>{account?.label || portfolio || '未标记账号'} 的日期文件夹</strong>
      <span>{account?.days.length || 0} 个日期 · 按北京时间从新到旧排列</span></div></div>
    <div className="archive-folder-grid">
      {account?.days.map(item => <button className="archive-folder-card" type="button" key={item.day} onClick={() => onDay(item.day)} aria-label={`打开日期文件夹 ${item.day}`}>
        <span className="archive-folder-icon" aria-hidden="true" />
        <span className="archive-folder-meta"><strong>{item.day}</strong>
          <small>{dayIdentity(`${item.day}T12:00:00+08:00`).weekday} · {item.folders.length} 个批次</small><small>{item.count} 张素材</small></span>
        <span className="archive-folder-arrow" aria-hidden="true">→</span>
      </button>)}
    </div>
    {!account && showEmpty && <p className="archive-empty" role="status">这个账号暂时没有素材。</p>}
  </>

  return <>
    <div className="archive-folder-summary"><div><strong>{day} 的批次文件夹</strong>
      <span>{date?.folders.length || 0} 个批次 · 按批次序号从小到大排列</span></div></div>
    <div className="archive-folder-grid">
      {date?.folders.map(folder => {
        const name = folder.folder || folder.day
        return <button className="archive-folder-card" type="button" key={name + '/' + (folder.batch || '')} onClick={() => onOpen(folder)}
          title={name} aria-label={`打开 ${name}，共 ${folder.count} 张合成图`}>
          <span className="archive-folder-icon" aria-hidden="true" />
          <span className="archive-folder-meta"><strong>{archiveBatchLabel(folder)}</strong><small>{folder.count} 张素材</small>
            {folder.latest_at && <small>最近入库 <time dateTime={folder.latest_at}>{historyTimeFormat.format(new Date(folder.latest_at))}</time></small>}
          </span>
          <span className="archive-folder-arrow" aria-hidden="true">→</span>
        </button>
      })}
    </div>
    {!date && showEmpty && <p className="archive-empty" role="status">这个日期暂时没有素材。</p>}
  </>
}
