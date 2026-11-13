import { useEffect, useState } from 'react'
import { archiveEndpoint as endpoint, checkArchiveResponse, readCompositionArchive, updateArchiveRecords, useCompositionArchive, type ArchiveEntry } from './compositionArchiveStore'

type Section = 'history' | 'trash'

const pageSize = 20
const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
})

export function CompositionArchive({ onClose, onPreview }: {
  onClose: () => void
  onPreview: (image: { url: string; name: string }) => void
}) {
  const [section, setSection] = useState<Section>('history')
  const { items, loaded, loading, error: readError } = useCompositionArchive()
  const [limit, setLimit] = useState(pageSize)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    void readCompositionArchive()
  }, [])

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

  const visible = items.filter((item) => item.section === section)
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
              <button className="toolbar-button" aria-pressed={section === tab} key={tab} type="button" onClick={() => { setSection(tab); setLimit(pageSize) }}>
                {tab === 'history' ? '合成记录' : '回收站'}
              </button>
            ))}
          </div>
          <button className="toolbar-button" type="button" onClick={() => { setError(''); void readCompositionArchive(true) }} disabled={loading || !!busy}>刷新</button>
        </div>
        <div className="archive-body" aria-busy={loading}>
          {(error || readError) && <p className="archive-error" role="alert">{error || readError}</p>}
          {loading && <p role="status">{loaded ? '正在更新记录…' : '正在读取 OSS…'}</p>}
          {visible.length ? (
            <>
              <div className="archive-grid">
                {visible.slice(0, limit).map((entry) => {
                  const url = `${endpoint}/${entry.id}/image`
                  const name = `${entry.template_name} · ${entry.product_name}`
                  return (
                    <article className="archive-card" key={entry.id}>
                      <button className="batch-image-button" type="button" onClick={() => onPreview({ url, name })} aria-label={`放大查看 ${name}`}>
                        <img src={url} alt={name} loading="lazy" />
                      </button>
                      <div className="archive-card-info">
                        <strong title={name}>{name}</strong>
                        <time dateTime={entry.expires_at}>{dateFormat.format(new Date(entry.expires_at))} 到期</time>
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
              {limit < visible.length && <button className="toolbar-button archive-more" type="button" onClick={() => setLimit((value) => value + pageSize)}>加载更多</button>}
            </>
          ) : (loaded || (!loading && !readError)) && !error && <p>{section === 'history' ? '下载合成图后，记录会出现在这里。' : '从合成结果删除的图片会出现在这里。'}</p>}
        </div>
      </section>
    </div>
  )
}
