import { useState } from 'react'
import { archiveEndpoint as endpoint, checkArchiveResponse, updateArchiveRecords, type ArchiveEntry } from './compositionArchiveStore'
import { dayIdentity } from './compositionArchiveFolders'
import { readArchiveQuery, useArchiveQuery } from './archiveQueries'

type Section = 'history' | 'trash'

const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
})
const savedTimeFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit' })

export function CompositionArchive({ onClose, onPreview, shared, onSignIn }: {
  onClose: () => void
  shared: boolean
  onSignIn: () => void
  onPreview: (image: { url: string; name: string }) => void
}) {
  const [section, setSection] = useState<Section>('history')
  const [openFolder, setOpenFolder] = useState<{name: string; batch?: string} | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const scope = shared ? 'team' : 'browser'
  const foldersQuery = `view=batch_folders&section=${section}&scope=${scope}`
  const folders = useArchiveQuery(foldersQuery)
  const pageKey = openFolder ? `view=page&section=${section}&scope=${scope}&folder=${encodeURIComponent(openFolder.name)}${openFolder.batch ? '&batch=' + openFolder.batch : ''}` : null
  const page = useArchiveQuery(pageKey)
  const groups = (folders.data.folders || []).map((group) => ({
    name: group.folder || group.day,
    batch: group.batch,
    weekday: dayIdentity(`${group.day}T12:00:00+08:00`).weekday,
    count: group.count,
  }))
  const visible = (page.data.items || []).filter((item) => Date.parse(item.expires_at) > Date.now())
  const loading = pageKey ? page.loading : folders.loading
  const loaded = pageKey ? page.loaded : folders.loaded
  const readError = pageKey ? page.error : folders.error

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
              <button className="toolbar-button" aria-pressed={section === tab} key={tab} type="button" onClick={() => { setSection(tab); setOpenFolder(null) }}>
                {tab === 'history' ? '合成记录' : '回收站'}
              </button>
            ))}
          </div>
          <button className="toolbar-button" type="button" onClick={() => { setError(''); void readArchiveQuery(foldersQuery, true); void readArchiveQuery(pageKey, true) }} disabled={loading || !!busy}>刷新</button>
        </div>
        <div className="archive-body" aria-busy={loading}>
          {!shared && <button className="toolbar-button" type="button" onClick={onSignIn}>登录或注册，查看全部共享记录</button>}
          {(error || readError) && <p className="archive-error" role="alert">{error || readError}</p>}
          {loading && <p role="status">{loaded ? '正在更新记录…' : '正在读取 OSS…'}</p>}
          {!openFolder ? (groups.length ? (
            <>
              <div className="archive-folder-summary">
                <div><strong>按批次整理</strong><span>{groups.length} 个文件夹 · 共 {groups.reduce((total, group) => total + group.count, 0)} 张合成图</span></div>
              </div>
              <div className="archive-folder-grid">
                {groups.map((group) => (
                  <button className="archive-folder-card" type="button" key={group.batch || group.name} onClick={() => { setOpenFolder(group) }} aria-label={`打开 ${group.name}，共 ${group.count} 张合成图`}>
                    <span className="archive-folder-icon" aria-hidden="true" />
                    <span className="archive-folder-meta"><strong>{group.name}</strong><small>{group.weekday} · {group.count} 张素材</small></span>
                    <span className="archive-folder-arrow" aria-hidden="true">→</span>
                  </button>
                ))}
              </div>
            </>
          ) : (loaded || (!loading && !readError)) && !error && (
            <p>{section === 'history' ? '下载合成图后，这里会按批次建立日期文件夹。' : '从合成结果删除的图片会按批次出现在这里。'}</p>
          )) : (
            <>
              <div className="archive-folder-heading">
                <button className="toolbar-button" type="button" onClick={() => { setOpenFolder(null) }}>← 返回批次文件夹</button>
                <div><strong>{openFolder.name}</strong><span>{page.data.total || 0} 张素材</span></div>
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
                        <time dateTime={entry.downloaded_at || entry.saved_at}>{savedTimeFormat.format(new Date(entry.downloaded_at || entry.saved_at))} 入库 · {dateFormat.format(new Date(entry.expires_at))} 到期</time>
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
