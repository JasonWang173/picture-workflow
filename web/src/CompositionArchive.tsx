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

export function CompositionArchive({ onClose, onPreview }: {
  onClose: () => void
  onPreview: (image: { url: string; name: string }) => void
}) {
  const [section, setSection] = useState<Section>('history')
  const [openDay, setOpenDay] = useState<string | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const folders = useArchiveQuery('view=folders')
  const pageKey = section === 'trash' ? 'view=page&section=trash' : openDay ? `view=page&section=history&day=${openDay}` : null
  const page = useArchiveQuery(pageKey)
  const historyGroups = (folders.data.folders || []).map((group) => ({ ...dayIdentity(`${group.day}T12:00:00+08:00`), count: group.count }))
  const activeGroup = openDay ? { ...dayIdentity(`${openDay}T12:00:00+08:00`), count: page.data.total || 0 } : undefined
  const visible = (page.data.items || []).filter((item) => Date.parse(item.expires_at) > Date.now())
  const loading = pageKey ? page.loading : folders.loading
  const loaded = pageKey ? page.loaded : folders.loaded
  const readError = pageKey ? page.error : folders.error

  const move = async (entry: ArchiveEntry, destination: Section) => {
    setBusy(entry.id)
    setError('')
    try {
      const restoring = destination === 'history'
      const response = await fetch(restoring ? `${endpoint}/${entry.id}/restore` : `${endpoint}/${entry.id}`,
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
          <div><h2 id="archive-title">合成空间站</h2><p>OSS 云端保存 · 每条记录保留 7 天</p></div>
          <button className="lightbox-close" type="button" onClick={onClose} aria-label="关闭合成空间站" autoFocus>×</button>
        </header>
        <div className="archive-toolbar">
          <div role="group" aria-label="记录分类">
            {(['history', 'trash'] as const).map((tab) => (
              <button className="toolbar-button" aria-pressed={section === tab} key={tab} type="button" onClick={() => { setSection(tab); setOpenDay(null) }}>
                {tab === 'history' ? '合成记录' : '回收站'}
              </button>
            ))}
          </div>
          <button className="toolbar-button" type="button" onClick={() => { setError(''); void readArchiveQuery('view=folders', true); void readArchiveQuery(pageKey, true) }} disabled={loading || !!busy}>刷新</button>
        </div>
        <div className="archive-body" aria-busy={loading}>
          {(error || readError) && <p className="archive-error" role="alert">{error || readError}</p>}
          {loading && <p role="status">{loaded ? '正在更新记录…' : '正在读取 OSS…'}</p>}
          {section === 'history' && !openDay ? (historyGroups.length ? (
            <>
              <div className="archive-folder-summary">
                <div><strong>按日期整理</strong><span>{historyGroups.length} 个文件夹 · 共 {historyGroups.reduce((total, group) => total + group.count, 0)} 张合成图</span></div>
                <small>点击文件夹查看当天下载过的素材</small>
              </div>
              <div className="archive-folder-grid">
                {historyGroups.map((group) => (
                  <button className="archive-folder-card" type="button" key={group.key} onClick={() => { setOpenDay(group.key) }} aria-label={`打开 ${group.label}，共 ${group.count} 张合成图`}>
                    <span className="archive-folder-icon" aria-hidden="true" />
                    <span className="archive-folder-meta"><strong>{group.label}</strong><small>{group.weekday} · {group.count} 张素材</small></span>
                    <span className="archive-folder-arrow" aria-hidden="true">→</span>
                  </button>
                ))}
              </div>
            </>
          ) : (loaded || (!loading && !readError)) && !error && <p>下载合成图后，系统会按日期自动建立文件夹。</p>) : (
            <>
              {section === 'history' && activeGroup && (
                <div className="archive-folder-heading">
                  <button className="toolbar-button" type="button" onClick={() => { setOpenDay(null) }}>← 返回日期文件夹</button>
                  <div><strong>{activeGroup.label}</strong><span>{activeGroup.weekday} · {activeGroup.count} 张素材</span></div>
                </div>
              )}
              <div className="archive-grid">
                {visible.map((entry) => {
                  const url = `${endpoint}/${entry.id}/image`
                  const name = `${entry.template_name} · ${entry.product_name}`
                  return (
                    <article className="archive-card" key={entry.id}>
                      <button className="batch-image-button" type="button" onClick={() => onPreview({ url, name })} aria-label={`放大查看 ${name}`}>
                        <img src={url} alt={name} loading="lazy" />
                      </button>
                      <div className="archive-card-info">
                        <strong title={name}>{name}</strong>
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
              {!visible.length && loaded && !error && !readError && <p>{section === 'history' ? '这个日期文件夹暂时没有素材。' : '从合成结果删除的图片会出现在这里。'}</p>}
            </>
          )}
        </div>
      </section>
    </div>
  )
}
