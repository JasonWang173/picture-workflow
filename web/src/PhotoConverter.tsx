import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import {
  ConversionRequestError, ConversionTask, conversionResponse, forgetRecentConversion, loadRecentConversion, saveRecentConversion,
  type RecentConversion, type UploadProgress,
} from './conversionRequest'

type Source = { index: number; name: string; width?: number; height?: number; frames?: number; input_format?: string; error?: string }
type Converted = { id: string; name: string; input_name: string; width: number; height: number; bytes: number; frame: number; url: string; download_url: string; warnings: string[] }
type State = { status: string; total: number; done: number; output_format?: 'jpeg' | 'png'; inputs?: Source[]; images?: Converted[]; skipped?: { index: number; name: string; error: string }[]; error?: string }
type Capabilities = { input_formats: string[]; heif: boolean; max_files: number; max_file_mb: number; max_batch_mb: number }
export const conversionAccept = 'image/*,.heic,.heif,.hif,.jfif,.avif,.tif,.tiff,.bmp,.gif,.webp,.ico,.jp2,.ppm,.pgm,.pbm,.pcx,.tga,.dds,.psd'

function sizeLabel(bytes: number) { return bytes >= 1024**2 ? `${(bytes/1024**2).toFixed(1)} MB` : `${Math.ceil(bytes/1024)} KB` }

export function FrameChoice({ job, source, value, onChange }: { job: string; source: Source; value: number; onChange: (frame: number) => void }) {
  const [draft, setDraft] = useState(value + 1)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState(false)
  const [version, setVersion] = useState(0)
  const choose = (number: number) => {
    const frame = Math.max(1, Math.min(source.frames || 1, Math.round(number || 1)))
    setDraft(frame)
    setLoaded(false)
    setError(false)
    setVersion((value) => value + 1)
    onChange(frame - 1)
  }
  return <article className="converter-frame-card">
    <div className="converter-frame-preview">
      {!loaded && !error && <span role="status">正在读取所选帧…</span>}
      {error && <span role="alert">此帧预览失败，可重新输入帧序号并点击预览</span>}
      <img key={`${value}-${version}`} loading="lazy" style={{ opacity: loaded ? 1 : 0 }} src={`/api/converter/${job}/frame/${source.index}/${value}?v=${version}`} alt={`${source.name} 第 ${value+1} 帧预览`} onLoad={() => setLoaded(true)} onError={() => { setError(true); setLoaded(false) }} />
    </div>
    <div><strong title={source.name}>{source.name}</strong><p>{source.input_format} · 共 {source.frames} 帧/页</p>
      <label>帧/页序号 <input type="number" min="1" max={source.frames} value={draft} onChange={(event) => setDraft(Number(event.target.value))} /></label>
      <div className="converter-frame-buttons">
        <button className="toolbar-button" type="button" disabled={value === 0} onClick={() => choose(value)}>上一帧</button>
        <button className="toolbar-button" type="button" onClick={() => choose(draft)}>预览并选择</button>
        <button className="toolbar-button" type="button" disabled={value + 1 === source.frames} onClick={() => choose(value + 2)}>下一帧</button>
      </div><small>当前选择第 {value + 1} 帧/页；修改序号后请点击“预览并选择”。缩略预览不改变导出尺寸。</small>
    </div>
  </article>
}

export function PhotoConverter({ onClose }: { onClose: () => void }) {
  const [files, setFiles] = useState<File[]>([])
  const [format, setFormat] = useState<'jpeg' | 'png'>('jpeg')
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [phase, setPhase] = useState('idle')
  const [busy, setBusy] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [upload, setUpload] = useState<UploadProgress | null>(null)
  const [state, setState] = useState<State | null>(null)
  const [sources, setSources] = useState<Source[]>([])
  const [frames, setFrames] = useState<Record<string, number>>({})
  const [results, setResults] = useState<Converted[]>([])
  const [skipped, setSkipped] = useState<State['skipped']>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [packing, setPacking] = useState(false)
  const [page, setPage] = useState(0)
  const [dragging, setDragging] = useState(false)
  const [recent, setRecent] = useState(loadRecentConversion)
  const taskRef = useRef<ConversionTask | null>(null)
  const resultJobRef = useRef<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const folderRef = useRef<HTMLInputElement>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    void fetch('/api/converter/capabilities', { signal: AbortSignal.timeout(15_000) }).then(conversionResponse)
      .then((value: Capabilities) => { if (alive.current) setCapabilities(value) })
      .catch(() => { if (alive.current) setError('转换器配置读取失败，请关闭后重试') })
    return () => { alive.current = false }
  }, [])

  const addFiles = (incoming: File[]) => {
    if (taskRef.current || busy || packing) return
    if (files.length + incoming.length > (capabilities?.max_files || 1000)) { setError('超出本批图片数量限制，请分批选择'); return }
    setFiles((previous) => [...previous, ...incoming])
    setError('')
  }
  const choose = (event: ChangeEvent<HTMLInputElement>) => { addFiles(Array.from(event.target.files || [])); event.target.value = '' }
  const drop = (event: DragEvent) => { event.preventDefault(); setDragging(false); addFiles(Array.from(event.dataTransfer.files)) }

  const poll = async (id: string) => {
    let failures = 0
    while (alive.current) {
      let next: State
      try { next = await conversionResponse(await fetch(`/api/converter/${id}`, { signal: AbortSignal.timeout(15_000) })) as State; failures = 0 }
      catch (cause) {
        if ((cause instanceof ConversionRequestError && cause.status === 404) || ++failures > 6) throw cause
        await new Promise((resolve) => setTimeout(resolve, 1000))
        continue
      }
      if (!alive.current) throw new Error('转换器已关闭')
      setState(next); setPhase(next.status); setUpload(null)
      if (['reserved', 'selecting', 'done', 'failed', 'cancelled'].includes(next.status)) return next
      await new Promise((resolve) => setTimeout(resolve, 750))
    }
    throw new Error('转换器已关闭')
  }
  const finished = (id: string, next: State) => {
    resultJobRef.current = id
    setResults(next.images || []); setSkipped(next.skipped || []); setPage(0)
    setPhase(next.status); taskRef.current = null
    setNotice(next.status === 'cancelled' ? `已取消转换，保留 ${next.images?.length || 0} 张已完成图片` : `已转换 ${next.images?.length || 0} 张图片`)
    if (next.error) setError(next.error)
  }
  const convert = async (id: string, selection: Record<string, number>) => {
    setPhase('queued'); setBusy(true); setError(''); setNotice('')
    setState((previous) => ({ status: 'queued', done: 0, total: previous?.total || files.length }))
    await conversionResponse(await fetch('/api/converter/start', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, frames: selection }), signal: AbortSignal.timeout(15_000) }))
    const next = await poll(id)
    finished(id, next)
  }
  const remember = (value: RecentConversion) => {
    saveRecentConversion(value)
    setRecent(value)
  }
  const follow = async (id: string, savedFrames: Record<string, number> = {}) => {
    const next = await poll(id)
    if (next.output_format) setFormat(next.output_format)
    if (next.status === 'reserved') {
      taskRef.current = null
      forgetRecentConversion(id)
      setRecent(null)
      setPhase('idle')
      throw new Error('上次上传未完成，请重新选择图片。')
    }
    if (next.status !== 'selecting') { finished(id, next); return }
    const inputs = next.inputs || []
    const selection = Object.fromEntries(inputs.map((source) => {
      const frame = savedFrames[source.index] ?? 0
      return [source.index, frame < (source.frames ?? 1) ? frame : 0]
    }))
    setSources(inputs); setFrames(selection)
    if (inputs.some((source) => !source.error && (source.frames || 1) > 1)) {
      setNotice('检测到动图或多页图片，请预览并选择要导出的帧/页，再确认转换。')
    } else await convert(id, selection)
  }
  const changeFrame = (index: number, frame: number) => {
    if (!taskRef.current?.id) return
    const selection = { ...frames, [index]: frame }
    setFrames(selection)
    remember({ id: taskRef.current.id, frames: selection })
  }
  const restore = async () => {
    if (!recent || busy) return
    const saved = loadRecentConversion() || recent
    taskRef.current = new ConversionTask(saved.id)
    setPhase('restoring'); setBusy(true); setState(null); setError(''); setNotice('')
    try { await follow(saved.id, saved.frames) }
    catch (cause) {
      if (!alive.current) return
      if (cause instanceof ConversionRequestError && cause.status === 404) {
        forgetRecentConversion(saved.id)
        setRecent(null)
        taskRef.current = null
        setPhase('idle')
        setError('最近一次转换已过期或已无法访问。')
      } else setError(cause instanceof Error ? cause.message : '恢复转换任务失败，请重试')
    } finally { if (alive.current) { setBusy(false); setCancelling(false) } }
  }
  const begin = async () => {
    if (taskRef.current || !capabilities || !files.length) return
    if (files.some((file) => file.size > capabilities.max_file_mb * 1024**2)) { setError(`单张图片不能超过 ${capabilities.max_file_mb} MB`); return }
    if (files.reduce((sum, file) => sum + file.size, 0) > capabilities.max_batch_mb * 1024**2) { setError(`本批文件总大小不能超过 ${capabilities.max_batch_mb} MB`); return }
    setBusy(true); setCancelling(false); setError(''); setNotice(''); setResults([]); setSkipped([]); setSources([]); setPhase('uploading'); setState(null)
    const task = new ConversionTask()
    taskRef.current = task
    try {
      const id = await task.upload(files, format, (value) => {
        if (alive.current) { setUpload(value); setPhase(value.preparing ? 'preparing' : 'uploading') }
      }, (id) => { if (alive.current) remember({ id, frames: {} }) })
      await follow(id)
    } catch (cause) {
      if (alive.current) setError(cause instanceof Error ? cause.message : '转换失败，请重试')
      if (!task.id) taskRef.current = null
    } finally { if (alive.current) { setBusy(false); setUpload(null); setCancelling(false) } }
  }
  const resume = async () => {
    if (!taskRef.current?.id) return
    try { await convert(taskRef.current.id, frames) }
    catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : '转换失败，请重试') }
    finally { if (alive.current) { setBusy(false); setCancelling(false) } }
  }
  const cancel = async () => {
    const task = taskRef.current
    if (!task) return
    setCancelling(true); setError('')
    try {
      await task.cancel()
      if (!busy && task.id && alive.current) {
        setBusy(true)
        try { finished(task.id, await poll(task.id)) }
        finally { if (alive.current) setBusy(false) }
      }
    } catch { if (alive.current) setError('取消请求未确认，请再次点击取消；任务可能仍在处理') }
    finally { if (alive.current) setCancelling(false) }
  }
  const downloadAll = async () => {
    if (!resultJobRef.current || packing) return
    setPacking(true); setError('')
    try {
      const result = await conversionResponse(await fetch(`/api/converter/${resultJobRef.current}/download`, { method: 'POST' }))
      const link = document.createElement('a'); link.href = result.url; link.download = 'converted-images.zip'; link.click()
    } catch (cause) { setError(cause instanceof Error ? cause.message : '打包失败，请重试') }
    finally { setPacking(false) }
  }

  const total = files.reduce((sum, file) => sum + file.size, 0)
  const locked = !!taskRef.current || busy || packing
  const canClose = !packing && !(busy && (phase === 'uploading' || phase === 'preparing'))
  const percentage = upload?.total ? Math.round(upload.loaded/upload.total*100) : state?.total ? Math.round(state.done/state.total*100) : null
  const titles: Record<string, string> = { uploading: '正在上传图片', preparing: '服务器准备任务', inspecting: '正在识别图片与帧数', queued: '等待转换', processing: '正在转换', cancelling: '正在取消转换', restoring: '正在恢复最近一次转换' }
  const pageCount = Math.max(1, Math.ceil(results.length/36))

  return <div className="lightbox converter-overlay">
    <section className="lightbox-panel converter-panel" role="dialog" aria-modal="true" aria-labelledby="converter-title">
      <header className="lightbox-bar"><div><h2 id="converter-title">照片格式转换器</h2><p>独立转换 · 不缩放、不裁剪、不主动旋转</p></div>
        <button className="lightbox-close" type="button" disabled={!canClose} onClick={onClose} aria-label="关闭格式转换器" title={canClose ? '关闭' : '请等待当前步骤完成'} autoFocus>×</button>
      </header>
      <div className="converter-body">
        <section className="converter-settings" aria-label="转换设置">
          <div className={`converter-drop ${dragging ? 'is-dragging' : ''}`} onDragOver={(event) => { event.preventDefault(); if (!locked) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={drop}>
            <strong>拖入照片，或批量选择图片 / 文件夹</strong>
            <span>支持 JPG、PNG、WebP、GIF、BMP、TIFF、AVIF{capabilities?.heif ? '、HEIC/HEIF' : ''} 等常用图片</span>
            <div className="converter-choice-buttons"><button type="button" className="toolbar-button" disabled={locked} onClick={() => inputRef.current?.click()}>选择图片</button>
              <button type="button" className="toolbar-button" disabled={locked} onClick={() => folderRef.current?.click()}>选择文件夹</button>
              <button type="button" className="toolbar-button" disabled={locked || !files.length} onClick={() => setFiles([])}>清空列表</button></div>
            <input type="file" multiple accept={conversionAccept} hidden ref={inputRef} onChange={choose} />
            <input type="file" multiple accept={conversionAccept} hidden {...{ webkitdirectory: '', directory: '' }} ref={folderRef} onChange={choose} />
          </div>
          <div className="converter-output"><fieldset disabled={locked}><legend>输出格式</legend>
            <label><input type="radio" name="converter-output" checked={format === 'jpeg'} onChange={() => setFormat('jpeg')} /> JPG / JPEG</label>
            <label><input type="radio" name="converter-output" checked={format === 'png'} onChange={() => setFormat('png')} /> PNG</label>
          </fieldset><p>{format === 'jpeg' ? 'JPEG 不支持透明背景，透明部分会填成白色；JPEG 编码为有损格式。' : 'PNG 保留透明背景，使用无损编码。'}</p>
            <p>保留原图尺寸和已有方向信息。动图、多页图片可选择帧/页，导出为静态图片。</p>
          </div>
        </section>
        <div className="converter-file-summary"><strong>{!files.length && state?.total ? `已恢复 ${state.total} 张图片的任务` : `${files.length} 张图片 · ${sizeLabel(total)}`}</strong><span>{capabilities ? `单图 ≤ ${capabilities.max_file_mb} MB · 单批 ≤ ${capabilities.max_files} 张` : '正在读取支持格式…'}</span></div>
        {files.length > 0 && <ol className="converter-file-list">{files.map((file, index) => <li key={`${index}-${file.name}`}><span>{index+1}</span><strong title={file.name}>{file.name}</strong><small>{sizeLabel(file.size)}</small><button type="button" className="toolbar-button" disabled={locked} onClick={() => setFiles((list) => list.filter((_, position) => position !== index))} aria-label={`移除 ${file.name}`}>×</button></li>)}</ol>}
        {error && <p className="converter-error" role="alert">{error}</p>}
        {notice && <p className="converter-notice" role="status">{notice}</p>}
        {(busy || cancelling) && <div className="converter-progress" role="status"><strong>{cancelling ? '正在取消转换' : titles[phase] || '正在处理'}</strong>
          {percentage !== null && !upload?.preparing && <><progress max="100" value={percentage} /><span>{percentage}% {upload?.total ? `· ${sizeLabel(upload.loaded)} / ${sizeLabel(upload.total)}` : state ? `· ${state.done} / ${state.total} 张` : ''}</span></>}
        </div>}
        {phase === 'selecting' && !busy && <section className="converter-frame-selection" aria-label="选择转换帧"><h3>选择动图帧 / 图片页</h3><p>单张静态图片按原图转换；动图和多页图片按下面选择的位置转换。</p>
          {sources.filter((source) => !source.error && (source.frames || 1)>1).map((source) => <FrameChoice key={source.index} source={source} job={taskRef.current!.id!} value={frames[source.index] ?? 0} onChange={(frame) => changeFrame(source.index, frame)} />)}
          {sources.filter((source) => source.error).map((source) => <p className="converter-error" key={source.index}>{source.name}：{source.error}</p>)}
        </section>}
        <div className="converter-actions">
          {!taskRef.current && <button type="button" className="compose-button" disabled={!capabilities || !files.length || busy || packing} onClick={() => void begin()}>开始转换为 {format === 'jpeg' ? 'JPG' : 'PNG'}</button>}
          {recent && !busy && !packing && !cancelling && phase !== 'selecting' && !results.length && <button type="button" className="toolbar-button" onClick={() => void restore()}>恢复最近一次转换</button>}
          {phase === 'selecting' && !busy && taskRef.current && <button type="button" className="compose-button" onClick={() => void resume()}>确认所选帧并开始转换</button>}
          {taskRef.current && <button type="button" className="toolbar-button cancel-task-button" disabled={cancelling} onClick={() => void cancel()}>{cancelling ? '正在取消…' : '取消转换'}</button>}
          {!!results.length && <button type="button" className="download-button" disabled={packing || busy} onClick={() => void downloadAll()}>{packing ? '正在打包…' : `下载全部 ${results.length} 张 ZIP`}</button>}
        </div>
        {!!skipped?.length && <div className="converter-error"><strong>{skipped.length} 张未能转换，其余结果已保留：</strong>{skipped.map((item) => <p key={item.index}>{item.name}：{item.error}</p>)}</div>}
        {!!results.length && <><div className="converter-results">{results.slice(page*36,(page+1)*36).map((image) => <article key={image.id}>
          <a href={image.url} target="_blank" rel="noopener"><img src={`${image.url}/thumb`} alt={image.input_name} loading="lazy" /></a>
          <strong title={image.input_name}>{image.input_name}</strong><span>{image.width} × {image.height} · {sizeLabel(image.bytes)}</span>
          {image.warnings.map((warning) => <small key={warning}>{warning}</small>)}<a className="download-button" href={image.download_url}>下载 {image.name.endsWith('.png') ? 'PNG' : 'JPG'}</a>
        </article>)}</div>{pageCount>1 && <div className="batch-pagination"><button type="button" className="toolbar-button" disabled={!page} onClick={() => setPage(page-1)}>上一页</button><span>第 {page+1} / {pageCount} 页</span><button type="button" className="toolbar-button" disabled={page+1===pageCount} onClick={() => setPage(page+1)}>下一页</button></div>}</>}
        <p className="converter-footnote">转换结果单独暂存 7 天，不进入模板库或合成记录。可在同一浏览器恢复最近一次任务。仅预览使用缩略图，导出保持原尺寸。不支持 SVG、PDF、相机 RAW 等非普通照片。</p>
      </div>
    </section>
  </div>
}
