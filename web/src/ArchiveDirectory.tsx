import { useMemo, useState } from 'react'
import type { ArchiveFolder } from './archiveQueries'
import { dayIdentity, historyTimeFormat } from './compositionArchiveFolders'

function FolderGrid({ folders, onOpen }: { folders: ArchiveFolder[]; onOpen: (folder: ArchiveFolder) => void }) {
  return (
    <div className="archive-folder-grid">
      {folders.map((folder) => {
        const name = folder.folder || folder.day
        return (
          <button className="archive-folder-card" type="button" key={folder.batch || name} onClick={() => onOpen(folder)} aria-label={`打开 ${name}，共 ${folder.count} 张合成图`}>
            <span className="archive-folder-icon" aria-hidden="true" />
            <span className="archive-folder-meta">
              <strong>{name}</strong>
              <small>{dayIdentity(`${folder.day}T12:00:00+08:00`).weekday} · {folder.count} 张素材</small>
              {folder.latest_at && <small>最近入库 <time dateTime={folder.latest_at}>{historyTimeFormat.format(new Date(folder.latest_at))}</time></small>}
            </span>
            <span className="archive-folder-arrow" aria-hidden="true">→</span>
          </button>
        )
      })}
    </div>
  )
}

export function ArchiveDirectory({ folders, section, portfolio, onSelect, onOpen, showEmpty }: {
  folders: ArchiveFolder[]
  section: 'history' | 'trash'
  portfolio: string | null
  onSelect: (username: string | null) => void
  onOpen: (folder: ArchiveFolder) => void
  showEmpty: boolean
}) {
  const [search, setSearch] = useState('')
  const query = search.trim().toLowerCase()
  const accounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const { username, count } of folders) {
      if (username) counts.set(username, (counts.get(username) || 0) + count)
    }
    return [...counts].sort(([a], [b]) => a.localeCompare(b, 'en'))
  }, [folders])

  if (portfolio) {
    const days = new Map<string, ArchiveFolder[]>()
    for (const folder of folders) {
      if (folder.username !== portfolio) continue
      const batches = days.get(folder.day)
      if (batches) batches.push(folder)
      else days.set(folder.day, [folder])
    }
    return (
      <>
        <div className="archive-folder-heading">
          <button className="toolbar-button" type="button" onClick={() => onSelect(null)}>← 全部合成记录</button>
          <h3>{portfolio} 作品集</h3>
        </div>
        {[...days].map(([day, batches]) => (
          <section className="archive-portfolio-day" aria-label={day} key={day}>
            <h4><time dateTime={day}>{day}</time></h4>
            <FolderGrid folders={batches} onOpen={onOpen} />
          </section>
        ))}
        {!days.size && showEmpty && <p>该账号暂无合成记录。</p>}
      </>
    )
  }

  const results = query ? accounts.filter(([username]) => username.startsWith(query)) : []
  const enter = (username: string) => {
    setSearch('')
    onSelect(username)
  }
  return (
    <>
      {section === 'history' && (
        <form className="archive-account-search" role="search" aria-label="搜索账号作品集" onSubmit={(event) => {
          event.preventDefault()
          const match = results.find(([username]) => username === query) || (results.length === 1 && results[0])
          if (match) enter(match[0])
        }}>
          <label htmlFor="archive-account-search">按账号查找作品集</label>
          <input id="archive-account-search" type="search" placeholder="输入账号，例如 zs" autoComplete="off"
            autoCapitalize="none" spellCheck={false} enterKeyHint="search" value={search} onChange={event => setSearch(event.target.value)} />
        </form>
      )}
      {query ? (
        <div className="archive-account-results">
          {results.map(([username, count]) => (
            <button className="toolbar-button" type="button" key={username} onClick={() => enter(username)}>
              {username} 作品集 <span>· {count} 张</span>
            </button>
          ))}
          {!results.length && showEmpty && <p role="status">没有找到与「{search.trim()}」匹配的账号作品集。</p>}
        </div>
      ) : folders.length ? (
        <>
          <div className="archive-folder-summary">
            <div><strong>{section === 'history' ? '日期从新到旧 · 同日按最近入库排序' : '按批次整理'}</strong><span>{folders.length} 个文件夹 · 共 {folders.reduce((total, folder) => total + folder.count, 0)} 张合成图</span></div>
          </div>
          <FolderGrid folders={folders} onOpen={onOpen} />
        </>
      ) : showEmpty && <p>{section === 'history' ? '下载合成图后，这里会按批次建立日期文件夹。' : '从合成结果删除的图片会按批次出现在这里。'}</p>}
    </>
  )
}
