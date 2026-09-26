import { ChangeEvent, DragEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'

type Mode = 'auto' | 'overlay' | 'cutout'
type Asset = { file: File; url: string }
type BatchImage = { id: string; name: string; url: string }
type LightboxImage = { url: string; name: string }

type IconName = 'arrow' | 'download' | 'image' | 'spark' | 'upload' | 'close' | 'check' | 'refresh' | 'zoom'

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
  onChange,
  onClear,
}: {
  number: string
  title: string
  hint: string
  assets: Asset[]
  accept: string
  allowFolder?: boolean
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
              {allowFolder && <div className="drop-actions">
                <button type="button" className="drop-choice" onClick={(event) => { event.stopPropagation(); choose() }}>选择图片</button>
                <button type="button" className="drop-choice" onClick={(event) => { event.stopPropagation(); chooseFolder() }}>选择文件夹</button>
              </div>}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function App() {
  const [products, setProducts] = useState<Asset[]>([])
  const [template, setTemplate] = useState<Asset | null>(null)
  const [mode, setMode] = useState<Mode>('auto')
  const [size, setSize] = useState('1080x1920')
  const [preserveLogo, setPreserveLogo] = useState(true)
  const [resultUrl, setResultUrl] = useState<string | null>(null)
  const [batchImages, setBatchImages] = useState<BatchImage[]>([])
  const [selectedBatchIds, setSelectedBatchIds] = useState<string[]>([])
  const [batchDownloadUrl, setBatchDownloadUrl] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [lastMode, setLastMode] = useState<Mode | null>(null)
  const [lastSize, setLastSize] = useState<string | null>(null)
  const [demoAvailable, setDemoAvailable] = useState(false)
  const [lightbox, setLightbox] = useState<LightboxImage | null>(null)
  const productsRef = useRef<Asset[]>([])
  const templateRef = useRef<Asset | null>(null)
  const resultRef = useRef<string | null>(null)

  useEffect(() => { productsRef.current = products }, [products])
  useEffect(() => { templateRef.current = template }, [template])
  useEffect(() => { resultRef.current = resultUrl }, [resultUrl])
  useEffect(() => {
    if (!lightbox) return
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setLightbox(null)
    }
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [lightbox])
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
  }, [])

  const resetResult = () => {
    setResultUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return null })
    setBatchImages([])
    setSelectedBatchIds([])
    setBatchDownloadUrl(null)
    setLightbox(null)
    setLastSize(null)
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
    setError('')
    resetResult()
  }
  const clearProducts = () => { products.forEach(releaseAsset); setProducts([]); resetResult() }
  const clearTemplate = () => { releaseAsset(template); setTemplate(null); resetResult() }

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
    if (!products.length || !template) return
    setLoading(true)
    setError('')
    try {
      const body = new FormData()
      products.forEach((asset) => body.append(products.length > 1 ? 'products' : 'product', asset.file))
      body.append('template', template.file)
      body.append('mode', mode)
      body.append('size', size)
      body.append('outline', '0.78')
      body.append('preserve_logo_pill', String(preserveLogo))
      body.append('quality', '100')
      const response = await fetch(products.length > 1 ? '/api/compose-batch' : '/api/compose', { method: 'POST', body })
      if (!response.ok) {
        let message = '合成失败，请检查图片后重试'
        try { const payload = await response.json(); message = payload.detail || message } catch { /* response may not be JSON */ }
        throw new Error(message)
      }
      if (products.length > 1) {
        const payload = await response.json() as { images: BatchImage[]; download_url: string }
        setBatchImages(payload.images)
        setSelectedBatchIds(payload.images.map((image) => image.id))
        setBatchDownloadUrl(payload.download_url)
        setResultUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return null })
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
  const isLandscape = displaySize === '1280x720'
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
        <p className="intro-copy">上传商品图与商家模板，交给合成引擎识别绿布尺寸。商品原图按比例高清嵌入，模板文字与 Logo 保持清晰。</p>
      </section>

      <section className="workspace" aria-label="图片合成工作台">
        <aside className="control-panel">
          <div className="panel-heading">
            <div><span className="section-label">01 / 素材</span><h2>放入两张图</h2></div>
            {demoAvailable && <button className="text-button" type="button" onClick={loadDemo}><Icon name="spark" size={14} /> 试用示例</button>}
          </div>
          <AssetDrop number="01" title="商品图" hint="支持多选图片或整个文件夹" assets={products} allowFolder accept="image/png,image/jpeg,image/webp" onChange={replaceProducts} onClear={clearProducts} />
          <AssetDrop number="02" title="模板图" hint="商家活动与品牌信息" assets={template ? [template] : []} accept="image/png,image/jpeg,image/webp" onChange={(files) => replaceTemplate(files[0])} onClear={clearTemplate} />

          <div className="settings-divider" />
          <div className="panel-heading compact"><div><span className="section-label">02 / 输出</span><h2>合成方式</h2></div></div>
          <div className="mode-list" role="radiogroup" aria-label="合成方式">
            {([
              ['auto', '智能判断', '自动识别模板类型'],
              ['overlay', '模板叠加', '适合白底活动模板'],
              ['cutout', '主体抠图', '绿布区域高清嵌入商品原图'],
            ] as [Mode, string, string][]).map(([value, label, description]) => (
              <button key={value} className={`mode-option ${mode === value ? 'is-selected' : ''}`} type="button" role="radio" aria-checked={mode === value} onClick={() => setMode(value)}>
                <span className="radio-dot" /><span><strong>{label}</strong><small>{description}</small></span>
              </button>
            ))}
          </div>
          <div className="settings-row">
            <label htmlFor="canvas-size">画布尺寸</label>
            <select id="canvas-size" value={size} onChange={(event) => setSize(event.target.value)}>
              <option value="1280x720">1280 × 720 · 横版</option>
              <option value="1080x1920">1080 × 1920 · 竖版 · 推荐</option>
              <option value="750x1334">750 × 1334 · 竖版</option>
              <option value="720x1280">720 × 1280 · 竖版</option>
            </select>
          </div>
          <label className="switch-row">
            <span><strong>保留 Logo 白色胶囊</strong><small>让品牌标识在复杂背景上更清楚</small></span>
            <input type="checkbox" checked={preserveLogo} onChange={(event) => setPreserveLogo(event.target.checked)} />
            <span className="switch-ui" aria-hidden="true"><i /></span>
          </label>
          {error && <div className="error-message" role="alert">{error}</div>}
          <button className="compose-button" type="button" disabled={!products.length || !template || loading} onClick={compose}>
            {loading ? <><span className="button-loader" /> 正在合成</> : <>{products.length > 1 ? `批量合成 ${products.length} 张` : '合成海报'} <Icon name="arrow" size={18} /></>}
          </button>
          <p className="privacy-note"><span className="lock-dot" /> 图片只在本地 Python 引擎中处理</p>
        </aside>

        <section className={`preview-panel ${resultUrl || hasBatch ? 'has-result' : ''}`} aria-live="polite">
          <div className="preview-topline">
            <div><span className="section-label">03 / 预览</span><h2>{resultUrl ? '合成完成' : hasBatch ? '批量合成完成' : '结果会出现在这里'}</h2></div>
            {(resultUrl || hasBatch) && <span className="result-badge"><Icon name="check" size={13} /> READY</span>}
          </div>
          <div className="preview-stage">
            {resultUrl ? (
              <button className="result-preview-button" type="button" onClick={() => setLightbox({ url: resultUrl, name: '合成海报.jpg' })} aria-label="点击放大查看合成海报">
                <img className="result-image" src={resultUrl} alt="合成后的海报" />
                <span className="preview-zoom-hint"><Icon name="zoom" size={14} /> 点击放大</span>
              </button>
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
                <strong>等待两张图片</strong>
                <span>上传后，预览将在这里展开</span>
              </div>
            )}
          </div>
          {resultUrl ? (
            <div className="result-actions">
              <div><span className="result-caption">输出尺寸</span><strong>{displaySize.replace('x', ' × ')} px · {lastMode === 'auto' ? '智能判断' : lastMode === 'overlay' ? '模板叠加' : '主体抠图 · 高清绿布替换'}</strong></div>
              <a className="download-button" href={resultUrl} download={`food-composite-${Date.now()}.jpg`}><Icon name="download" size={16} /> 下载 JPG</a>
            </div>
          ) : hasBatch ? (
            <div className="result-actions">
              <div><span className="result-caption">批量输出</span><strong>{displaySize.replace('x', ' × ')} px · {batchImages.length} 张 JPG</strong></div>
              <div className="batch-download-actions">
                <a className={`download-button ${selectedDownloadUrl ? '' : 'is-disabled'}`} href={selectedDownloadUrl || '#'} onClick={(event) => { if (!selectedDownloadUrl) event.preventDefault() }} download="food-composites-selected.zip"><Icon name="download" size={16} /> 下载选中</a>
                <a className="download-button download-button-secondary" href={batchDownloadUrl || '#'} onClick={(event) => { if (!batchDownloadUrl) event.preventDefault() }} download="food-composites.zip"><Icon name="download" size={16} /> 下载全部</a>
              </div>
            </div>
          ) : (
            <div className="preview-footnote"><span>TIP</span> {mode === 'cutout' ? '检测到绿布时只替换占位区域，商品原图按比例高清嵌入，不拉伸、不额外模糊。' : '白底活动模板会自动转为透明图层，商品图作为真实背景铺满画布。'}</div>
          )}
        </section>
      </section>
      <footer className="footer"><span>FRAME/FOOD</span><span>Built for fast food campaigns · 本地图片工作流</span></footer>
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
