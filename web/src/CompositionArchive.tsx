import { useState } from 'react'
import { archiveEndpoint as endpoint, checkArchiveResponse, updateArchiveRecords, type ArchiveEntry } from './compositionArchiveStore'
import { archiveBatchLabel, historyTimeFormat } from './compositionArchiveFolders'
import { folderQueryKey, readArchiveQuery, useArchiveQuery, type ArchiveFolder } from './archiveQueries'
import { ArchiveDirectory } from './ArchiveDirectory'

type Section = 'history' | 'trash'

const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
})
const savedTimeFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit' })

export function CompositionArchive({ onClose, onPreview, username, onSignIn }: {
  onClose: () => void
  username?: string
  onSignIn: () => void
  onPreview: (image: { url: string; name: string }) => void
}) {
  const [section, setSection] = useState<Section>('history')
  const [openFolder, setOpenFolder] = useState<ArchiveFolder | null>(null)
  const [portfolio, setPortfolio] = useState<string | null>(null)
  const [openDay, setOpenDay] = useState<string | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const shared = !!username
  const scope = shared ? 'team' : 'browser'
  const foldersQuery = folderQueryKey(section, username)
  const folders = useArchiveQuery(foldersQuery)
  const pageKey = openFolder ? `view=page&section=${section}&scope=${scope}${username ? '&viewer=' + encodeURIComponent(username) : ''}&folder=${encodeURIComponent(openFolder.folder || openFolder.day)}${openFolder.batch ? '&batch=' + openFolder.batch : ''}${section === 'history' ? '&order=recent' : ''}` : null
  const page = useArchiveQuery(pageKey)
  const visible = (page.data.items || []).filter((item) => Date.parse(item.expires_at) > Date.now())
  const loading = pageKey ? page.loading : folders.loading
  const loaded = pageKey ? page.loaded : folders.loaded
  const readError = pageKey ? page.error : folders.error
  const syncing = pageKey ? page.syncing : folders.syncing

  const selectAccount = (value: string | null) => {
    setPortfolio(value)
    setOpenDay(null)
    setOpenFolder(null)
    setError('')
  }
  const selectDay = (value: string | null) => {
    setOpenDay(value)
    setOpenFolder(null)
    setError('')
  }
  const goUp = () => {
    if (openFolder) setOpenFolder(null)
    else if (openDay) selectDay(null)
    else selectAccount(null)
  }

  const move = async (entry: ArchiveEntry, destination: Section) => {
    setBusy(entry.id)
    setError('')
    try {
      const restoring = destination === 'history'
      const ref = entry.archive_key || entry.id
      const response = await fetch(restoring ? `${endpoint}/${ref}/restore` : `${endpoint}/${ref}`,
        { method: restoring ? 'POST' : 'DELETE' })
      await checkArchiveResponse(response, '操作失败，请重试')
      const { item } = await response.json() as { item: ArchiveEntry }
      updateArchiveRecords([item])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '操作失败，请重试')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="lightbox" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <section className="lightbox-panel archive-panel" role="dialog" aria-modal="true" aria-labelledby="archive-title">
        <header className="lightbox-bar">
          <div><h2 id="archive-title">合成空间站</h2><p>{shared ? '所有账号共享' : '当前浏览器的记录'} · OSS 保留 7 天</p></div>
          <button className="lightbox-close" type="button" onClick={onClose} aria-label="关闭合成空间站" autoFocus>×</button>
        </header>
        <div className="archive-toolbar">
          <div role="group" aria-label="记录分类">
            {(['history', 'trash'] as const).map((tab) => (
              <button className="toolbar-button" aria-pressed={section === tab} key={tab} type="button" onClick={() => { setSection(tab); selectAccount(null) }}>
                {tab === 'history' ? '合成记录' : '回收站'}
              </button>
            ))}
          </div>
          <button className="toolbar-button" type="button" onClick={async () => {
            setError('')
            await readArchiveQuery(foldersQuery, 'refresh')
            await readArchiveQuery(pageKey, true)
          }} disabled={loading || folders.loading || !!busy}>刷新</button>
        </div>
        <div className="archive-body" aria-busy={loading}>
          {!shared && <button className="toolbar-button" type="button" onClick={onSignIn}>登录或注册，查看全部共享记录</button>}
          <div className="archive-navigation">
            {portfolio !== null && <button className="toolbar-button" type="button" onClick={goUp}>← 返回上一级</button>}
            <nav aria-label="空间站文件夹路径">
              <ol className="archive-breadcrumbs">
                <li>{portfolio === null ? <span aria-current="page">全部账号</span> : <button type="button" onClick={() => selectAccount(null)}>全部账号</button>}</li>
                {portfolio !== null && <li>{openDay === null ? <span aria-current="page">{portfolio || '未标记账号'}</span>
                  : <button type="button" onClick={() => selectDay(null)}>{portfolio || '未标记账号'}</button>}</li>}
                {openDay && <li>{openFolder ? <button type="button" onClick={() => setOpenFolder(null)}>{openDay}</button>
                  : <span aria-current="page">{openDay}</span>}</li>}
                {openFolder && <li><span aria-current="page">{archiveBatchLabel(openFolder)}</span></li>}
              </ol>
            </nav>
          </div>
          {error && <p className="archive-error" role="alert">{error}</p>}
          {readError && (loaded && !pageKey
            ? <p role="status">正在显示上次的目录，暂时无法同步最新记录。</p>
            : <p className="archive-error" role="alert">{readError}</p>)}
          {syncing ? <p role="status">{loaded ? '正在同步最新目录…' : '正在同步合成目录，完成后会自动显示。'}</p>
            : loading && <p role="status">{loaded ? '正在更新目录…' : '正在打开目录…'}</p>}
          {!openFolder ? (
            <ArchiveDirectory key={section} folders={folders.data.folders || []} portfolio={portfolio} day={openDay}
              onSelect={selectAccount} onDay={selectDay} onOpen={setOpenFolder} showEmpty={loaded && !error && !readError} />
          ) : (
            <>
              <div className="archive-folder-heading">
                <div><strong>{openFolder.folder || openFolder.day}</strong><span>{page.data.total || 0} 张素材{section === 'history' && ' · 最近入库在前（北京时间）'}</span></div>
              </div>
              <div className="archive-grid">
                {visible.map((entry) => {
                  const ref = entry.archive_key || entry.id
                  const url = `${endpoint}/${ref}/image`
                  const name = entry.name
                  return (
                    <article className="archive-card" key={ref}>
                      <button className="batch-image-button" type="button" onClick={() => onPreview({ url, name })} aria-label={`放大查看 ${name}`}>
                        <img src={url} alt={name} loading="lazy" />
                      </button>
                      <div className="archive-card-info">
                        <strong title={name}>{name}</strong>
                        <span className="archive-card-template" title={entry.template_name}>模板 {entry.template_name}</span>
                        <time dateTime={entry.downloaded_at || entry.saved_at}>{(section === 'history' ? historyTimeFormat : savedTimeFormat).format(new Date(entry.downloaded_at || entry.saved_at))} 入库 · {dateFormat.format(new Date(entry.expires_at))} 到期</time>
                        {section === 'history' ? (
                          <div className="archive-card-actions">
                            <a className="toolbar-button" href={`${url}?download=true`} target="_blank" rel="noopener">下载 JPG</a>
                            <button className="toolbar-button" type="button" onClick={() => void move(entry, 'trash')} disabled={!!busy}>移入回收站</button>
                          </div>
                        ) : (
                          <button className="toolbar-button" type="button" onClick={() => void move(entry, 'history')} disabled={!!busy}>{busy === entry.id ? '正在恢复…' : '恢复'}</button>
                        )}
                      </div>
                    </article>
                  )
                })}
              </div>
              {page.data.next_cursor && <button className="toolbar-button archive-more" type="button" disabled={loading} onClick={() => void readArchiveQuery(pageKey, false, true)}>{loading ? '正在加载…' : '加载更多'}</button>}
              {!visible.length && loaded && !error && !readError && <p>这个文件夹暂时没有素材。</p>}
            </>
          )}
        </div>
      </section>
    </div>
  )
}
