export type UploadProgress = { loaded: number; total: number | null; preparing: boolean }
export type RecentConversion = { id: string; frames: Record<string, number> }

const recentKey = 'picture-workflow.recent-conversion.v1'
const isJobId = (id: unknown): id is string => typeof id === 'string' && /^[a-f0-9]{16}$/.test(id)

export function loadRecentConversion(): RecentConversion | null {
  try {
    const recent = JSON.parse(localStorage.getItem(recentKey) || 'null')
    if (!isJobId(recent?.id)) return null
    const entries = Object.entries(recent.frames || {}).filter((entry): entry is [string, number] => {
      const [index, frame] = entry
      return /^[1-9]\d{0,3}$/.test(index) && typeof frame === 'number' && Number.isSafeInteger(frame) && frame >= 0
    })
    return { id: recent.id, frames: Object.fromEntries(entries) }
  } catch { return null }
}

export function saveRecentConversion(recent: RecentConversion) {
  try { localStorage.setItem(recentKey, JSON.stringify(recent)) } catch { /* Storage may be disabled. */ }
}

export function forgetRecentConversion(id: string) {
  try { if (loadRecentConversion()?.id === id) localStorage.removeItem(recentKey) } catch { /* Storage may be disabled. */ }
}

export class ConversionRequestError extends Error {
  constructor(public readonly status: number, message: string) { super(message) }
}

export async function conversionResponse(response: Response) {
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new ConversionRequestError(response.status, typeof payload?.detail === 'string' ? payload.detail : `转换请求失败（${response.status}）`)
  return payload
}

export class ConversionTask {
  constructor(public id: string | null = null) {}
  requestedCancel = false
  private uploadRequest: XMLHttpRequest | null = null
  private pendingCancel: Promise<void> | null = null

  async upload(files: File[], outputFormat: 'jpeg' | 'png', onProgress: (value: UploadProgress) => void, onReserved?: (id: string) => void) {
    const reserve = await conversionResponse(await fetch('/api/converter/reserve', { method: 'POST', signal: AbortSignal.timeout(15_000) }))
    if (!isJobId(reserve?.id)) throw new Error('服务器未返回有效转换任务')
    this.id = reserve.id
    onReserved?.(reserve.id)
    if (this.requestedCancel) { await this.cancel(); return this.id! }
    const body = new FormData()
    body.set('job', this.id!)
    body.set('output_format', outputFormat)
    files.forEach((file) => body.append('files', file))
    try {
      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest()
        this.uploadRequest = xhr
        let settled = false
        let finishedUpload = false
        const fail = (message: string) => { if (!settled) { settled = true; reject(new Error(message)) } }
        xhr.open('POST', '/api/converter/upload')
        xhr.responseType = 'json'
        xhr.timeout = 30 * 60 * 1000
        xhr.upload.onprogress = (event) => {
          if (!settled && !finishedUpload) onProgress({ loaded: event.loaded, total: event.lengthComputable ? event.total : null, preparing: false })
        }
        xhr.upload.onload = () => { if (!settled) { finishedUpload = true; onProgress({ loaded: 0, total: null, preparing: true }) } }
        xhr.onerror = () => fail('图片上传时网络中断，请重试')
        xhr.onabort = () => fail('上传已取消')
        xhr.ontimeout = () => fail('图片上传超时，请重试')
        xhr.onload = () => {
          if (settled) return
          if (xhr.status < 200 || xhr.status >= 300) {
            fail(typeof xhr.response?.detail === 'string' ? xhr.response.detail : `转换上传失败（${xhr.status}）`)
            return
          }
          settled = true
          resolve()
        }
        onProgress({ loaded: 0, total: null, preparing: false })
        xhr.send(body)
      })
    } catch (error) {
      if (!this.requestedCancel) throw error
      await this.cancel()
    } finally {
      this.uploadRequest = null
    }
    return this.id!
  }

  cancel(): Promise<void> {
    this.requestedCancel = true
    this.uploadRequest?.abort()
    if (!this.id) return Promise.resolve()
    if (this.pendingCancel) return this.pendingCancel
    this.pendingCancel = fetch(`/api/converter/${this.id}/cancel`, { method: 'POST', signal: AbortSignal.timeout(15_000) })
      .then(conversionResponse).then(() => undefined).finally(() => { this.pendingCancel = null })
    return this.pendingCancel
  }
}
