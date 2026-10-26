import { ChangeEvent, DragEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'

type Mode = 'auto' | 'overlay' | 'cutout' | 'template_replace'
type Asset = { file: File; url: string }
type BatchImage = { id: string; name: string; url: string }
type LightboxImage = { url: string; name: string }
type TemplateRecord = { id: string; name: string; url: string; thumb_url?: string; source?: string; created_at?: string; size?: number }
type SlotRect = { x: number; y: number; w: number; h: number }

type IconName = 'arrow' | 'download' | 'image' | 'spark' | 'upload' | 'close' | 'check' | 'refresh' | 'zoom' | 'library'

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const common = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, 'aria-hidden': true }
  const paths: Record<IconName, React.ReactNode> = {
    arrow: <><path d="M5 12h13" /><path d="m13 6 6 6-6 6" /></>,
    download: <><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 20h14" /></>,
    image: <><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="8.5" cy="9" r="1.4" /><path d="m4 17 5-5 3.5 3.5 2.5-2.5 5 5" /></>,
    spark: <><path d="m12 3 1.35 5.65L19 10l-5.65 1.35L12 17l-1.35-5.65L5 10l5.65-1.35L12 3Z" /><path d="m19 16 .55 2.45L22 19l-2.45.55L19 22l-.55-2.45L16 19l2.45-.55L19 16Z" /></>,
    upload: <><path d="M12 16V4" /><path d="m7 9 5-5 5 5" /><path d="M5 20h14" /></>,
    close: <><path d="m6 6 12 12" /><path d="m18 6-12 12" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    refresh: <><path d="M20 11a8 8 0 0 0-14.5-4L4 9" /><path d="M4 4v5h5" /><path d="M4 13a8 8 0 0 0 14.5 4L20 15" /><path d="M20 20v-5h-5" /></>,
    zoom: <><path d="M8 3H3v5" /><path d="M16 3h5v5" /><path d="M8 21H3v-5" /><path d="M21 16v5h-5" /></>,
    library: <><path d="M4 5h16" /><path d="M4 12h16" /><path d="M4 19h16" /><path d="M7 5v14" /><path d="M17 5v14" /></>,
  }
  return <svg {...common}>{paths[name]}</svg>
}

function makeAsset(file: File): Asset {
  return { file, url: URL.createObjectURL(file) }
}

function releaseAsset(asset: Asset | null) {
  if (asset) URL.revokeObjectURL(asset.url)
}

function AssetDrop({
  number,
  title,
  hint,
  assets,
  accept,
  allowFolder = false,
  onPickFromLibrary,
  onChange,
  onClear,
}: {
  number: string
  title: string
  hint: string
  assets: Asset[]
  accept: string
  allowFolder?: boolean
  onPickFromLibrary?: () => void
  onChange: (files: File[]) => void
  onClear: () => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const hasAssets = assets.length > 0

  const choose = () => inputRef.current?.click()
  const chooseFolder = () => folderInputRef.current?.click()
  const handleInput = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []).filter((file) => file.type.startsWith('image/'))
    if (files.length) onChange(allowFolder ? files : [files[0]])
    event.target.value = ''
  }
  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    const files = Array.from(event.dataTransfer.files || []).filter((file) => file.type.startsWith('image/'))
    if (files.length) onChange(allowFolder ? files : [files[0]])
  }
  const handleKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      choose()
    }
  }

  return (
    <div className="asset-block">
      <div className="asset-heading">
        <span className="step-number">{number}</span>
        <div>
          <strong>{title}</strong>
          <span>{hint}</span>
        </div>
      </div>
      <div
        className={`drop-zone ${dragging ? 'is-dragging' : ''} ${hasAssets ? 'has-assets' : ''}`}
        onClick={hasAssets ? undefined : choose}
        onDragEnter={(event) => { event.preventDefault(); setDragging(true) }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onKeyDown={hasAssets ? undefined : handleKey}
        role={hasAssets ? undefined : 'button'}
        tabIndex={hasAssets ? -1 : 0}
      >
        <input ref={inputRef} type="file" accept={accept} multiple={allowFolder} onChange={handleInput} hidden />
        {allowFolder && <input ref={folderInputRef} type="file" accept={accept} multiple {...{ webkitdirectory: '', directory: '' }} onChange={handleInput} hidden />}
        {hasAssets ? (
          <>
            <div className={`asset-grid ${assets.length === 1 ? 'single' : ''}`}>
              {assets.slice(0, 6).map((asset) => <img key={asset.url} src={asset.url} alt={`${title}预览`} />)}
              {assets.length > 6 && <span className="asset-overflow">+{assets.length - 6}</span>}
            </div>
            <div className="asset-meta">
              <span className="asset-status"><Icon name="check" size={13} /> {allowFolder ? `${assets.length} 张图片已载入` : '已载入'}</span>
              <span className="asset-name" title={assets[0].file.name}>{allowFolder ? '批量素材 · 可批量合成' : assets[0].file.name}</span>
              {onPickFromLibrary && (
                <button type="button" className="asset-meta-pick" onClick={(event) => { event.stopPropagation(); onPickFromLibrary() }}>
                  <Icon name="library" size={11} /> 换模板 · 从模板库选择
                </button>
              )}
            </div>
            <button className="icon-button asset-remove" type="button" onClick={(event) => { event.stopPropagation(); onClear() }} aria-label={`移除${title}`}>
              <Icon name="close" size={15} />
            </button>
          </>
        ) : (
          <div className="drop-prompt">
            <span className="upload-mark"><Icon name="upload" size={20} /></span>
            <div>
              <strong>{allowFolder ? '拖入图片，或选择图片 / 文件夹' : '拖入图片，或点击选择'}</strong>
              <span>{allowFolder ? '支持多选或整文件夹 · 合成后下载 ZIP' : 'PNG / JPG / WEBP · 最大 24 MB'}</span>
              {(allowFolder || onPickFromLibrary) && <div className="drop-actions">
                {allowFolder && <>
                  <button type="button" className="drop-choice" onClick={(event) => { event.stopPropagation(); choose() }}>选择图片</button>
                  <button type="button" className="drop-choice" onClick={(event) => { event.stopPropagation(); chooseFolder() }}>选择文件夹</button>
                </>}
                {onPickFromLibrary && (
                  <button type="button" className="drop-choice" onClick={(event) => { event.stopPropagation(); onPickFromLibrary() }}>
                    <Icon name="library" size={12} /> 从模板库选择
                  </button>
                )}
              </div>}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function TemplateLibrary({
  templates,
  selectedId,
  loading,
  busy,
  notice,
  onUpload,
  onSelect,
  onDelete,
}: {
  templates: TemplateRecord[]
  selectedId: string | null
  loading: boolean
  busy: boolean
  notice: string
  onUpload: (files: File[]) => void
  onSelect: (template: TemplateRecord) => void
  onDelete: (template: TemplateRecord) => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const handleInput = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []).filter((file) => file.type.startsWith('image/'))
    if (files.length) onUpload(files)
    event.target.value = ''
  }
  return (
    <section className="template-library" aria-label="模板库">
      <div className="library-heading">
        <div className="library-title"><span className="library-icon"><Icon name="library" size={15} /></span><div><strong>模板库</strong><small>{loading ? '正在读取模板…' : `${templates.length} 个模板可用 · 点卡片即可选用`}</small></div></div>
        <>
          <input ref={inputRef} type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={handleInput} hidden />
          <button className="library-upload" type="button" disabled={busy} onClick={() => inputRef.current?.click()}><Icon name="upload" size={13} /> {busy ? '保存中' : '上传模板'}</button>
        </>
      </div>
      {notice && <p className="library-notice"><Icon name="check" size={11} /> {notice}</p>}
      {templates.length ? (
        <div className="template-grid">
          {templates.map((item) => (
            <article className={`template-card ${selectedId === item.id ? 'is-selected' : ''}`} key={item.id}>
              <button className="template-card-select" type="button" onClick={() => onSelect(item)} aria-label={`使用模板 ${item.name}`}>
                <img src={item.thumb_url || item.url} alt={item.name} loading="lazy" />
                <span className="template-card-check"><Icon name="check" size={11} /></span>
              </button>
              <div className="template-card-meta"><strong title={item.name}>{item.name}</strong><small>{item.source === 'template_replace' ? '模板替换生成' : '已上传模板'}</small></div>
              <button className="template-card-delete" type="button" onClick={() => onDelete(item)} aria-label={`删除模板 ${item.name}`}><Icon name="close" size={12} /></button>
            </article>
          ))}
        </div>
      ) : (
        <button className="template-library-empty" type="button" onClick={() => inputRef.current?.click()}>
          <Icon name="upload" size={16} /><span>上传常用模板，之后可直接选择使用</span>
        </button>
      )}
    </section>
  )
}

function TemplatePickerModal({
  templates,
  selectedId,
  loading,
  busy,
  notice,
  error,
  onPick,
  onUpload,
  onClose,
}: {
  templates: TemplateRecord[]
  selectedId: string | null
  loading: boolean
  busy: boolean
  notice: string
  error: string
  onPick: (template: TemplateRecord) => void
  onUpload: (files: File[]) => void
  onClose: () => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const handleInput = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []).filter((file) => file.type.startsWith('image/'))
    if (files.length) onUpload(files)
    event.target.value = ''
  }
  return (
    <div className="lightbox template-picker" role="dialog" aria-modal="true" aria-label="从模板库选择模板" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <div className="lightbox-panel picker-panel">
        <div className="lightbox-bar">
          <div>
            <span className="section-label">TEMPLATE LIBRARY</span>
            <strong>{loading ? '正在读取模板…' : `选择一个模板用于当前合成 · 共 ${templates.length} 个`}</strong>
          </div>
          <div className="picker-actions">
            <input ref={inputRef} type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={handleInput} hidden />
            <button className="library-upload" type="button" disabled={busy} onClick={() => inputRef.current?.click()}><Icon name="upload" size={13} /> {busy ? '保存中' : '上传模板'}</button>
            <button className="lightbox-close" type="button" onClick={onClose} aria-label="关闭模板选择器"><Icon name="close" size={19} /></button>
          </div>
        </div>
        {error ? <p className="picker-message is-error">{error}</p> : notice ? <p className="picker-message">{notice}</p> : null}
        <div className="picker-stage">
          {templates.length ? (
            <div className="picker-grid">
              {templates.map((item) => (
                <article className={`template-card ${selectedId === item.id ? 'is-selected' : ''}`} key={item.id}>
                  <button className="template-card-select" type="button" disabled={busy} onClick={() => onPick(item)} aria-label={`使用模板 ${item.name}`}>
                    <img src={item.thumb_url || item.url} alt={item.name} loading="lazy" />
                    <span className="template-card-check"><Icon name="check" size={11} /></span>
                  </button>
                  <div className="template-card-meta"><strong title={item.name}>{item.name}</strong><small>{item.source === 'template_replace' ? '模板替换生成' : '已上传模板'}</small></div>
                </article>
              ))}
            </div>
          ) : (
            <button className="template-library-empty picker-empty" type="button" onClick={() => inputRef.current?.click()}>
              <Icon name="upload" size={16} /><span>模板库还是空的 · 点击上传常用模板</span>
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

type SlotDragMode = 'new' | 'move' | 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w'

const SLOT_HANDLES: { id: SlotDragMode; left: string; top: string; cursor: string }[] = [
  { id: 'nw', left: '0%', top: '0%', cursor: 'nwse-resize' },
  { id: 'n', left: '50%', top: '0%', cursor: 'ns-resize' },
  { id: 'ne', left: '100%', top: '0%', cursor: 'nesw-resize' },
  { id: 'e', left: '100%', top: '50%', cursor: 'ew-resize' },
  { id: 'se', left: '100%', top: '100%', cursor: 'nwse-resize' },
  { id: 's', left: '50%', top: '100%', cursor: 'ns-resize' },
  { id: 'sw', left: '0%', top: '100%', cursor: 'nesw-resize' },
  { id: 'w', left: '0%', top: '50%', cursor: 'ew-resize' },
]

const clamp01 = (value: number) => Math.min(1, Math.max(0, value))

function SlotEditorModal({
  imageUrl,
  imageName,
  initial,
  onConfirm,
  onClose,
}: {
  imageUrl: string
  imageName: string
  initial: SlotRect | null
  onConfirm: (rect: SlotRect) => void
  onClose: () => void
}) {
  const [rect, setRect] = useState<SlotRect | null>(initial)
  const stageRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ mode: SlotDragMode; start: { x: number; y: number }; origin: SlotRect } | null>(null)

  const toNorm = (event: React.PointerEvent) => {
    const box = stageRef.current?.getBoundingClientRect()
    if (!box || box.width === 0 || box.height === 0) return null
    return {
      x: clamp01((event.clientX - box.left) / box.width),
      y: clamp01((event.clientY - box.top) / box.height),
    }
  }
  const beginDrag = (mode: SlotDragMode, event: React.PointerEvent) => {
    const point = toNorm(event)
    if (!point) return
    event.preventDefault()
    stageRef.current?.setPointerCapture(event.pointerId)
    dragRef.current = { mode, start: point, origin: rect ?? { x: point.x, y: point.y, w: 0, h: 0 } }
    if (mode === 'new') setRect({ x: point.x, y: point.y, w: 0, h: 0 })
  }
  const handleDown = (event: React.PointerEvent) => {
    const point = toNorm(event)
    if (!point) return
    const inside = rect !== null && point.x >= rect.x && point.x <= rect.x + rect.w && point.y >= rect.y && point.y <= rect.y + rect.h
    beginDrag(inside ? 'move' : 'new', event)
  }
  const handleMove = (event: React.PointerEvent) => {
    const drag = dragRef.current
    if (!drag) return
    const point = toNorm(event)
    if (!point) return
    const { mode, start, origin } = drag
    if (mode === 'new') {
      setRect({ x: Math.min(start.x, point.x), y: Math.min(start.y, point.y), w: Math.abs(point.x - start.x), h: Math.abs(point.y - start.y) })
      return
    }
    if (mode === 'move') {
      const w = Math.min(origin.w, 1)
      const h = Math.min(origin.h, 1)
      setRect({ x: clamp01(Math.min(origin.x + point.x - start.x, 1 - w)), y: clamp01(Math.min(origin.y + point.y - start.y, 1 - h)), w, h })
      return
    }
    let x0 = origin.x
    let y0 = origin.y
    let x1 = origin.x + origin.w
    let y1 = origin.y + origin.h
    if (mode.includes('w')) x0 = point.x
    if (mode.includes('e')) x1 = point.x
    if (mode.includes('n')) y0 = point.y
    if (mode.includes('s')) y1 = point.y
    if (x1 - x0 < 0.02) { if (mode.includes('w')) x0 = x1 - 0.02; else x1 = x0 + 0.02 }
    if (y1 - y0 < 0.02) { if (mode.includes('n')) y0 = y1 - 0.02; else y1 = y0 + 0.02 }
    x0 = clamp01(x0); x1 = clamp01(x1); y0 = clamp01(y0); y1 = clamp01(y1)
    if (x1 - x0 < 0.02) x1 = Math.min(1, x0 + 0.02)
    if (y1 - y0 < 0.02) y1 = Math.min(1, y0 + 0.02)
    setRect({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 })
  }
  const handleUp = () => {
    const drag = dragRef.current
    dragRef.current = null
    if (drag?.mode === 'new') setRect((current) => (current && (current.w < 0.02 || current.h < 0.02) ? null : current))
  }
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return
      event.preventDefault()
      const step = event.shiftKey ? 0.002 : 0.01
      setRect((current) => {
        if (!current) return current
        const { w, h } = current
        let { x, y } = current
        if (event.key === 'ArrowLeft') x = clamp01(Math.min(x - step, 1 - w))
        if (event.key === 'ArrowRight') x = clamp01(Math.min(x + step, 1 - w))
        if (event.key === 'ArrowUp') y = clamp01(Math.min(y - step, 1 - h))
        if (event.key === 'ArrowDown') y = clamp01(Math.min(y + step, 1 - h))
        return { x, y, w, h }
      })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const usable = rect !== null && rect.w >= 0.02 && rect.h >= 0.02
  return (
    <div className="lightbox slot-editor" role="dialog" aria-modal="true" aria-label="手动框选商品区域" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <div className="lightbox-panel picker-panel">
        <div className="lightbox-bar">
          <div>
            <span className="section-label">MANUAL SLOT</span>
            <strong>框选商品图片区 · {imageName}</strong>
          </div>
          <button className="lightbox-close" type="button" onClick={onClose} aria-label="关闭框选">
            <Icon name="close" size={19} />
          </button>
        </div>
        <div className="slot-editor-stage">
          <div
            ref={stageRef}
            className="slot-stage"
            onPointerDown={handleDown}
            onPointerMove={handleMove}
            onPointerUp={handleUp}
            onPointerCancel={handleUp}
          >
            <img src={imageUrl} alt="模板框选预览" draggable={false} />
            {rect && (
              <span
                className="slot-rect"
                style={{ left: `${rect.x * 100}%`, top: `${rect.y * 100}%`, width: `${rect.w * 100}%`, height: `${rect.h * 100}%` }}
                onPointerDown={(event) => { event.stopPropagation(); beginDrag('move', event) }}
              >
                {SLOT_HANDLES.map((handle) => (
                  <span
                    key={handle.id}
                    className="slot-handle"
                    style={{ left: handle.left, top: handle.top, cursor: handle.cursor }}
                    onPointerDown={(event) => { event.stopPropagation(); beginDrag(handle.id, event) }}
                  />
                ))}
              </span>
            )}
          </div>
        </div>
        <div className="slot-editor-actions">
          <span>{usable && rect
            ? `已框选 ${Math.round(rect.w * 100)}% × ${Math.round(rect.h * 100)}% · 框内拖动移动 · 拖手柄缩放 · 方向键微调 · 后台将自动补全到框所在的完整商品区`
            : '在空白处拖拽画框；画好后可拖动框内移动、拖角点/边手柄缩放'}</span>
          <div className="picker-actions">
            <button className="library-upload" type="button" onClick={onClose}>取消</button>
            <button className="library-upload slot-confirm" type="button" disabled={!usable} onClick={() => rect && onConfirm(rect)}>使用此区域</button>
          </div>
        </div>
      </div>
    </div>
  )
}

function App() {
  const [products, setProducts] = useState<Asset[]>([])
  const [template, setTemplate] = useState<Asset | null>(null)
  const [mode, setMode] = useState<Mode>('template_replace')
  const [size, setSize] = useState('1080x1920')
  const [preserveLogo, setPreserveLogo] = useState(true)
  const [resultUrl, setResultUrl] = useState<string | null>(null)
  const [batchImages, setBatchImages] = useState<BatchImage[]>([])
  const [batchProgress, setBatchProgress] = useState<{ done: number; total: number } | null>(null)
  const [selectedBatchIds, setSelectedBatchIds] = useState<string[]>([])
  const [batchDownloadUrl, setBatchDownloadUrl] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [lastMode, setLastMode] = useState<Mode | null>(null)
  const [lastSize, setLastSize] = useState<string | null>(null)
  const [demoAvailable, setDemoAvailable] = useState(false)
  const [lightbox, setLightbox] = useState<LightboxImage | null>(null)
  const [templates, setTemplates] = useState<TemplateRecord[]>([])
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null)
  const [libraryLoading, setLibraryLoading] = useState(true)
  const [libraryBusy, setLibraryBusy] = useState(false)
  const [libraryNotice, setLibraryNotice] = useState('')
  const [pickerOpen, setPickerOpen] = useState(false)
  const [manualSlot, setManualSlot] = useState<SlotRect | null>(null)
  const [slotEditorOpen, setSlotEditorOpen] = useState(false)
  const [resultSavedName, setResultSavedName] = useState<string | null>(null)
  const productsRef = useRef<Asset[]>([])
  const templateRef = useRef<Asset | null>(null)
  const resultRef = useRef<string | null>(null)
  const noticeTimerRef = useRef<number | null>(null)

  useEffect(() => { productsRef.current = products }, [products])
  useEffect(() => { templateRef.current = template }, [template])
  useEffect(() => { resultRef.current = resultUrl }, [resultUrl])
  const flashNotice = (message: string) => {
    setLibraryNotice(message)
    if (noticeTimerRef.current !== null) window.clearTimeout(noticeTimerRef.current)
    noticeTimerRef.current = window.setTimeout(() => setLibraryNotice(''), 3600)
  }
  const loadTemplates = async () => {
    try {
      const response = await fetch('/api/templates')
      if (!response.ok) throw new Error('模板库读取失败')
      const payload = await response.json() as { templates?: TemplateRecord[] }
      setTemplates(payload.templates || [])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '模板库读取失败')
    } finally {
      setLibraryLoading(false)
    }
  }
  useEffect(() => { loadTemplates() }, [])
  useEffect(() => {
    if (!lightbox && !pickerOpen && !slotEditorOpen) return
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setLightbox(null)
        setPickerOpen(false)
        setSlotEditorOpen(false)
      }
    }
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [lightbox, pickerOpen, slotEditorOpen])
  useEffect(() => {
    fetch('/api/demo-available')
      .then((response) => response.ok ? response.json() : { available: false })
      .then((payload: { available: boolean }) => setDemoAvailable(payload.available))
      .catch(() => setDemoAvailable(false))
  }, [])
  useEffect(() => () => {
    productsRef.current.forEach(releaseAsset)
    releaseAsset(templateRef.current)
    if (resultRef.current) URL.revokeObjectURL(resultRef.current)
    if (noticeTimerRef.current !== null) window.clearTimeout(noticeTimerRef.current)
  }, [])

  const resetResult = () => {
    setResultUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return null })
    setBatchImages([])
    setBatchProgress(null)
    setSelectedBatchIds([])
    setBatchDownloadUrl(null)
    setLightbox(null)
    setLastSize(null)
    setResultSavedName(null)
  }
  const replaceProducts = (files: File[]) => {
    products.forEach(releaseAsset)
    setProducts(files.map(makeAsset))
    setError('')
    resetResult()
  }
  const replaceTemplate = (file: File) => {
    releaseAsset(template)
    setTemplate(makeAsset(file))
    setSelectedTemplateId(null)
    setManualSlot(null)
    setError('')
    resetResult()
  }
  const clearProducts = () => { products.forEach(releaseAsset); setProducts([]); resetResult() }
  const clearTemplate = () => { releaseAsset(template); setTemplate(null); resetResult() }
  const chooseMode = (nextMode: Mode) => {
    setMode(nextMode)
    setError('')
    resetResult()
  }

  const uploadTemplates = async (files: File[]) => {
    setLibraryBusy(true)
    setError('')
    try {
      for (const file of files) {
        const body = new FormData()
        body.append('template', file)
        body.append('name', file.name.replace(/\.[^.]+$/, '') || '未命名模板')
        body.append('source', 'upload')
        const response = await fetch('/api/templates', { method: 'POST', body })
        if (!response.ok) {
          let message = '模板保存失败'
          try { const payload = await response.json(); message = payload.detail || message } catch { /* response may not be JSON */ }
          throw new Error(message)
        }
      }
      await loadTemplates()
      flashNotice(files.length > 1 ? `已上传 ${files.length} 个模板` : `已上传模板“${files[0].name.replace(/\.[^.]+$/, '')}”`)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '模板保存失败')
    } finally {
      setLibraryBusy(false)
    }
  }

  const selectLibraryTemplate = async (item: TemplateRecord): Promise<boolean> => {
    setLibraryBusy(true)
    setError('')
    try {
      const response = await fetch(item.url)
      if (!response.ok) throw new Error('模板读取失败')
      const blob = await response.blob()
      const suffix = blob.type === 'image/png' ? '.png' : blob.type === 'image/webp' ? '.webp' : '.jpg'
      replaceTemplate(new File([blob], `${item.name}${suffix}`, { type: blob.type || 'image/jpeg' }))
      setSelectedTemplateId(item.id)
      flashNotice(`已选用模板“${item.name}”`)
      return true
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '模板读取失败')
      return false
    } finally {
      setLibraryBusy(false)
    }
  }

  const pickFromLibrary = async (item: TemplateRecord) => {
    const picked = await selectLibraryTemplate(item)
    if (picked) setPickerOpen(false)
  }

  const deleteLibraryTemplate = async (item: TemplateRecord) => {
    if (!window.confirm(`确定从模板库删除“${item.name}”吗？`)) return
    setLibraryBusy(true)
    setError('')
    try {
      const response = await fetch(`/api/templates/${item.id}`, { method: 'DELETE' })
      if (!response.ok) throw new Error('模板删除失败')
      if (selectedTemplateId === item.id) setSelectedTemplateId(null)
      await loadTemplates()
      flashNotice(`已删除“${item.name}”`)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '模板删除失败')
    } finally {
      setLibraryBusy(false)
    }
  }

  const saveResultToLibrary = async () => {
    if (!resultUrl || resultSavedName) return
    const originalName = template?.file.name.replace(/\.[^.]+$/, '') || '模板'
    const defaultName = `${originalName}-空白模板`
    const requested = window.prompt('存入模板库 · 给模板起个名字', defaultName)
    if (requested === null) return
    const cleanName = requested.trim().slice(0, 80) || defaultName
    setLibraryBusy(true)
    setError('')
    try {
      const blobResponse = await fetch(resultUrl)
      const blob = await blobResponse.blob()
      const body = new FormData()
      body.append('template', new File([blob], `${cleanName}.jpg`, { type: 'image/jpeg' }))
      body.append('name', cleanName)
      body.append('source', 'template_replace')
      const response = await fetch('/api/templates', { method: 'POST', body })
      if (!response.ok) throw new Error('生成模板保存失败')
      await loadTemplates()
      setResultSavedName(cleanName)
      flashNotice(`已存入模板库：${cleanName}`)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '生成模板保存失败')
    } finally {
      setLibraryBusy(false)
    }
  }

  const loadDemo = async () => {
    setError('')
    try {
      const [productResponse, templateResponse] = await Promise.all([
        fetch('/api/demo-assets/product'),
        fetch('/api/demo-assets/template'),
      ])
      if (!productResponse.ok || !templateResponse.ok) throw new Error('示例素材不可用')
      const [productBlob, templateBlob] = await Promise.all([productResponse.blob(), templateResponse.blob()])
      replaceProducts([new File([productBlob], '商品示例.png', { type: productBlob.type })])
      replaceTemplate(new File([templateBlob], '模板示例.jpg', { type: templateBlob.type }))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加载示例失败')
    }
  }

  const compose = async () => {
    const needsProduct = mode !== 'template_replace'
    const isBatch = needsProduct && products.length > 1
    if (!template || (needsProduct && !products.length)) return
    setLoading(true)
    setError('')
    setResultSavedName(null)
    try {
      const body = new FormData()
      if (needsProduct) products.forEach((asset) => body.append(isBatch ? 'products' : 'product', asset.file))
      body.append('template', template.file)
      body.append('mode', mode)
      body.append('size', size)
      body.append('outline', '0.78')
      body.append('preserve_logo_pill', String(preserveLogo))
      body.append('quality', '100')
      if (mode === 'template_replace' && manualSlot) {
        body.append('slot', `${manualSlot.x.toFixed(4)},${manualSlot.y.toFixed(4)},${manualSlot.w.toFixed(4)},${manualSlot.h.toFixed(4)}`)
      }
      const response = await fetch(isBatch ? '/api/compose-batch' : '/api/compose', { method: 'POST', body })
      if (!response.ok) {
        let message = '合成失败，请检查图片后重试'
        try { const payload = await response.json(); message = payload.detail || message } catch { /* response may not be JSON */ }
        throw new Error(message)
      }
      if (isBatch) {
        const payload = await response.json() as { batch_id: string; total: number; status_url: string }
        setBatchImages([])
        setSelectedBatchIds([])
        setBatchDownloadUrl(null)
        setResultUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return null })
        setBatchProgress({ done: 0, total: payload.total })
        let statusFailures = 0
        for (;;) {
          await new Promise((resolve) => setTimeout(resolve, 1500))
          const statusResponse = await fetch(payload.status_url)
          if (!statusResponse.ok) {
            statusFailures += 1
            if (statusFailures > 6) throw new Error('批次状态读取失败')
            continue
          }
          statusFailures = 0
          const status = await statusResponse.json() as {
            status: string
            done: number
            total: number
            images: BatchImage[]
            download_url: string
            error?: string | null
          }
          setBatchProgress({ done: status.done, total: status.total })
          if (status.status !== 'done' && status.status !== 'failed') continue
          setBatchProgress(null)
          setBatchImages(status.images || [])
          setSelectedBatchIds((status.images || []).map((image) => image.id))
          setBatchDownloadUrl(status.download_url || null)
          if (status.status === 'failed') throw new Error(status.error || '批量合成失败')
          if (!(status.images || []).length) throw new Error(status.error || '没有图片合成成功，请检查模板与商品图')
          break
        }
      } else {
        const blob = await response.blob()
        setResultUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return URL.createObjectURL(blob) })
        setBatchImages([])
        setSelectedBatchIds([])
        setBatchDownloadUrl(null)
      }
      setLastMode(mode)
      setLastSize(size)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '合成失败，请重试')
    } finally {
      setLoading(false)
    }
  }

  const hasBatch = batchImages.length > 0
  const displaySize = lastSize || size
  const [displayWidth, displayHeight] = displaySize.split('x').map(Number)
  const isLandscape = displayWidth > displayHeight
  const allBatchSelected = hasBatch && selectedBatchIds.length === batchImages.length
  const selectedDownloadUrl = batchDownloadUrl && selectedBatchIds.length
    ? `${batchDownloadUrl}?ids=${encodeURIComponent(selectedBatchIds.join(','))}`
    : null
  const selectAllBatch = () => setSelectedBatchIds(batchImages.map((image) => image.id))
  const clearBatchSelection = () => setSelectedBatchIds([])
  const toggleBatchSelection = (id: string) => {
    setSelectedBatchIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Frame Food 首页">
          <span className="brand-mark"><Icon name="spark" size={16} /></span>
          <span>FRAME<span>/</span>FOOD</span>
        </a>
        <div className="topbar-right">
          <span className="engine-status"><i /> Python compositor</span>
          <span className="version">v0.1</span>
        </div>
      </header>

      <section className="intro-row">
        <div>
          <p className="eyebrow">PRODUCT PHOTO STUDIO</p>
          <h1>把一张食物图，<br /><em>变成可以投放的海报。</em></h1>
        </div>
        <p className="intro-copy">先用“模板替换”清理网上模板，再用“主体抠图合成”放入自己的商品图。尺寸、版式和文字都会保留。</p>
      </section>

      <section className="workspace" aria-label="图片合成工作台">
        <aside className="control-panel">
          <div className="panel-heading">
            <div><span className="section-label">01 / 工作流</span><h2>{mode === 'template_replace' ? '先制作空白模板' : '合成商品海报'}</h2></div>
            {demoAvailable && <button className="text-button" type="button" onClick={loadDemo}><Icon name="spark" size={14} /> 试用示例</button>}
          </div>
          <TemplateLibrary templates={templates} selectedId={selectedTemplateId} loading={libraryLoading} busy={libraryBusy} notice={libraryNotice} onUpload={uploadTemplates} onSelect={selectLibraryTemplate} onDelete={deleteLibraryTemplate} />
          <div className="library-divider" />
          {mode === 'template_replace' ? (
            <div className="template-only-note"><span className="template-only-icon">✦</span><div><strong>无需上传商品图</strong><small>只清除模板中的商品图片区，保留其他设计</small></div></div>
          ) : (
            <AssetDrop number="01" title="商品图" hint="支持多选图片或整个文件夹" assets={products} allowFolder accept="image/png,image/jpeg,image/webp" onChange={replaceProducts} onClear={clearProducts} />
          )}
          {mode === 'template_replace' && template && (
            <div className="slot-row">
              <button className="drop-choice" type="button" onClick={() => setSlotEditorOpen(true)}>
                <Icon name="zoom" size={12} /> {manualSlot ? '重新框选商品区' : '手动框选商品区'}
              </button>
              {manualSlot ? (
                <>
                  <span className="slot-chip">已框选 {Math.round(manualSlot.w * 100)}% × {Math.round(manualSlot.h * 100)}%</span>
                  <button className="slot-clear" type="button" onClick={() => setManualSlot(null)}>清除，恢复自动识别</button>
                </>
              ) : (
                <span className="slot-hint-inline">自动识别不准时，可拖拽画个大致范围，后台自动补全到完整商品区并精修边界</span>
              )}
            </div>
          )}
          <AssetDrop number="02" title="模板图" hint="商家活动与品牌信息 · 可从模板库选择" assets={template ? [template] : []} accept="image/png,image/jpeg,image/webp" onPickFromLibrary={() => setPickerOpen(true)} onChange={(files) => replaceTemplate(files[0])} onClear={clearTemplate} />

          <div className="settings-divider" />
          <div className="panel-heading compact"><div><span className="section-label">02 / 输出</span><h2>合成方式</h2></div></div>
          <div className="mode-list" role="radiogroup" aria-label="合成方式">
            {([
              ['template_replace', '1 · 模板替换', '自动扣掉商品图，生成绿色占位模板'],
              ['cutout', '2 · 主体抠图合成', '上传商品图 + 空白模板，按比例嵌入'],
              ['auto', '智能判断', '兼容旧流程，自动选择处理方式'],
              ['overlay', '模板叠加', '保留 PNG 透明层，叠加 Logo 与文案'],
            ] as [Mode, string, string][]).map(([value, label, description]) => (
              <button key={value} className={`mode-option workflow-option ${mode === value ? 'is-selected' : ''}`} type="button" role="radio" aria-checked={mode === value} onClick={() => chooseMode(value)}>
                <span className="radio-dot" /><span><strong>{label}</strong><small>{description}</small></span>
              </button>
            ))}
          </div>
          <div className="settings-row">
            <label htmlFor="canvas-size">画布尺寸</label>
            <select id="canvas-size" value={size} onChange={(event) => setSize(event.target.value)}>
              <option value="1080x1920">1080 × 1920 · 竖版</option>
              <option value="1280x720">1280 × 720 · 横版</option>
            </select>
          </div>
          <label className="switch-row">
            <span><strong>补强 Logo 白色胶囊</strong><small>透明 PNG 保留原设计；普通模板可自动补底</small></span>
            <input type="checkbox" checked={preserveLogo} onChange={(event) => setPreserveLogo(event.target.checked)} />
            <span className="switch-ui" aria-hidden="true"><i /></span>
          </label>
          {error && <div className="error-message" role="alert">{error}</div>}
          <button className="compose-button" type="button" disabled={!template || (mode !== 'template_replace' && !products.length) || loading} onClick={compose}>
            {loading ? <><span className="button-loader" /> 正在处理</> : <>{mode === 'template_replace' ? '生成空白模板' : products.length > 1 ? `批量合成 ${products.length} 张` : '合成海报'} <Icon name="arrow" size={18} /></>}
          </button>
          <p className="privacy-note"><span className="lock-dot" /> 图片只在本地 Python 引擎中处理</p>
        </aside>

        <section className={`preview-panel ${resultUrl || hasBatch ? 'has-result' : ''}`} aria-live="polite">
          <div className="preview-topline">
            <div><span className="section-label">03 / 预览</span><h2>{resultUrl ? '合成完成' : batchProgress ? '批量合成中' : hasBatch ? '批量合成完成' : '结果会出现在这里'}</h2></div>
            {(resultUrl || hasBatch) && <span className="result-badge"><Icon name="check" size={13} /> READY</span>}
          </div>
          <div className="preview-stage">
            {resultUrl ? (
              <button className="result-preview-button" type="button" onClick={() => setLightbox({ url: resultUrl, name: '合成海报.jpg' })} aria-label="点击放大查看合成海报">
                <img className="result-image" src={resultUrl} alt="合成后的海报" />
                <span className="preview-zoom-hint"><Icon name="zoom" size={14} /> 点击放大</span>
              </button>
            ) : batchProgress ? (
              <div className="batch-progress">
                <span className="batch-progress-icon"><Icon name="refresh" size={26} /></span>
                <strong>正在后台合成 {batchProgress.done} / {batchProgress.total} 张</strong>
                <div className="batch-progress-bar"><i style={{ width: `${batchProgress.total ? Math.round((batchProgress.done / batchProgress.total) * 100) : 0}%` }} /></div>
                <span>数量不再受限 · 服务器后台处理中，保持页面打开即可</span>
              </div>
            ) : hasBatch ? (
              <div className="batch-preview">
                <div className="batch-toolbar">
                  <div className="batch-summary"><strong>{batchImages.length} 张合成图</strong><span>已选 {selectedBatchIds.length} 张</span></div>
                  <div className="batch-toolbar-actions">
                    <button type="button" className="toolbar-button" onClick={selectAllBatch} disabled={allBatchSelected}>全选</button>
                    <button type="button" className="toolbar-button" onClick={clearBatchSelection} disabled={!selectedBatchIds.length}>取消全选</button>
                  </div>
                </div>
                <div className={`batch-grid ${isLandscape ? 'is-landscape' : 'is-portrait'}`}>
                  {batchImages.map((image, index) => {
                    const selected = selectedBatchIds.includes(image.id)
                    return (
                      <article className={`batch-card ${selected ? 'is-selected' : ''}`} key={image.id}>
                        <label className="batch-check" aria-label={`${selected ? '取消选择' : '选择'} ${image.name}`}>
                          <input type="checkbox" checked={selected} onChange={() => toggleBatchSelection(image.id)} />
                          <span><Icon name="check" size={12} /></span>
                        </label>
                        <button className="batch-image-button" type="button" onClick={() => setLightbox({ url: image.url, name: image.name })} aria-label={`放大查看 ${image.name}`}>
                          <img src={image.url} alt={`${index + 1} ${image.name}`} />
                          <span className="preview-zoom-hint"><Icon name="zoom" size={13} /></span>
                        </button>
                        <div className="batch-card-meta">
                          <span>{String(index + 1).padStart(2, '0')}</span>
                          <span title={image.name}>{image.name}</span>
                          <a href={image.url} download={image.name} aria-label={`下载 ${image.name}`}><Icon name="download" size={14} /></a>
                        </div>
                      </article>
                    )
                  })}
                </div>
              </div>
            ) : (
              <div className="empty-preview">
                <div className="empty-frame"><span className="empty-corner corner-tl" /><span className="empty-corner corner-tr" /><span className="empty-corner corner-bl" /><span className="empty-corner corner-br" /><Icon name="image" size={30} /></div>
                <strong>{mode === 'template_replace' ? '等待模板图' : '等待两张图片'}</strong>
                <span>{mode === 'template_replace' ? '无需上传商品图，处理后生成空白模板' : '上传后，预览将在这里展开'}</span>
              </div>
            )}
          </div>
          {resultUrl ? (
            <div className="result-actions">
              <div><span className="result-caption">输出尺寸</span><strong>{displaySize.replace('x', ' × ')} px · JPG <span className="size-limit">&lt;300KB</span> · {lastMode === 'auto' ? '智能判断' : lastMode === 'overlay' ? '模板叠加 · Logo/文字' : lastMode === 'template_replace' ? '模板替换 · 绿色占位模板' : '主体抠图 · 区域替换'}</strong></div>
              <div className="result-download-actions">{lastMode === 'template_replace' && (resultSavedName
                ? <span className="download-button save-template-button is-saved"><Icon name="check" size={15} /> 已存入模板库</span>
                : <button className="download-button save-template-button" type="button" disabled={libraryBusy} onClick={saveResultToLibrary}><Icon name="library" size={15} /> {libraryBusy ? '保存中' : '存入模板库'}</button>)}<a className="download-button" href={resultUrl} download={`food-composite-${Date.now()}.jpg`}><Icon name="download" size={16} /> 下载 JPG</a></div>
            </div>
          ) : hasBatch ? (
            <div className="result-actions">
              <div><span className="result-caption">批量输出</span><strong>{displaySize.replace('x', ' × ')} px · {batchImages.length} 张 JPG · 单张 &lt;300KB</strong></div>
              <div className="batch-download-actions">
                <a className={`download-button ${selectedDownloadUrl ? '' : 'is-disabled'}`} href={selectedDownloadUrl || '#'} onClick={(event) => { if (!selectedDownloadUrl) event.preventDefault() }} download="food-composites-selected.zip"><Icon name="download" size={16} /> 下载选中</a>
                <a className="download-button download-button-secondary" href={batchDownloadUrl || '#'} onClick={(event) => { if (!batchDownloadUrl) event.preventDefault() }} download="food-composites.zip"><Icon name="download" size={16} /> 下载全部</a>
              </div>
            </div>
          ) : (
              <div className="preview-footnote"><span>TIP</span> {mode === 'cutout' ? '主体抠图会识别绿色占位区，只替换该区域，商品原图按比例高清嵌入。' : mode === 'overlay' ? '模板叠加优先使用 PNG 原生透明层；没有透明层时才识别边缘连通底色。商品图按比例铺满，不拉伸变形。' : mode === 'template_replace' ? '模板替换无需商品素材：只清除模板里的商品图片区，输出绿色占位模板。自动识别不准时用“手动框选商品区”画个大致范围，后台会自动补全到完整商品区并精修边界。' : '智能判断会优先使用模板透明层，再识别商品占位区域，商品图按比例铺满。'}</div>
          )}
        </section>
      </section>
      <footer className="footer"><span>FRAME/FOOD</span><span>Built for fast food campaigns · 本地图片工作流</span></footer>
      {pickerOpen && (
        <TemplatePickerModal
          templates={templates}
          selectedId={selectedTemplateId}
          loading={libraryLoading}
          busy={libraryBusy}
          notice={libraryNotice}
          error={error}
          onPick={pickFromLibrary}
          onUpload={uploadTemplates}
          onClose={() => setPickerOpen(false)}
        />
      )}
      {slotEditorOpen && template && (
        <SlotEditorModal
          imageUrl={template.url}
          imageName={template.file.name}
          initial={manualSlot}
          onConfirm={(next) => { setManualSlot(next); setSlotEditorOpen(false) }}
          onClose={() => setSlotEditorOpen(false)}
        />
      )}
      {lightbox && (
        <div className="lightbox" role="dialog" aria-modal="true" aria-label="合成图放大预览" onMouseDown={(event) => { if (event.target === event.currentTarget) setLightbox(null) }}>
          <div className="lightbox-panel">
            <div className="lightbox-bar">
              <div><span className="section-label">DETAIL PREVIEW</span><strong>{lightbox.name}</strong></div>
              <button className="lightbox-close" type="button" onClick={() => setLightbox(null)} aria-label="关闭放大预览"><Icon name="close" size={19} /></button>
            </div>
            <div className="lightbox-stage"><img src={lightbox.url} alt={`放大查看 ${lightbox.name}`} /></div>
          </div>
        </div>
      )}
    </main>
  )
}

export default App
