export type SubmissionProgress = {
  stage: 'uploading' | 'preparing'
  loaded: number
  total: number | null
}

export type BatchSubmission = { batch_id: string; total: number; status_url: string }

// Fetch does not expose upload progress. XHR reports bytes actually sent by
// the browser, separately from waiting for the server to create the task.
export function submitComposition(body: FormData, onProgress: (progress: SubmissionProgress) => void, signal?: AbortSignal): Promise<BatchSubmission> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    let loaded = 0
    let total: number | null = null
    let uploaded = false
    let settled = false
    const abort = () => { request.abort(); fail('上传已取消') }
    const fail = (message: string) => {
      if (settled) return
      settled = true
      signal?.removeEventListener('abort', abort)
      reject(new Error(message))
    }
    request.open('POST', '/api/compose-batch')
    request.timeout = 30 * 60 * 1000
    request.responseType = 'json'
    request.upload.onprogress = (event) => {
      if (uploaded || settled) return
      loaded = event.loaded
      total = event.lengthComputable && event.total > 0 ? event.total : null
      onProgress({ stage: 'uploading', loaded, total })
    }
    request.upload.onload = () => {
      if (settled) return
      uploaded = true
      if (total !== null) loaded = total
      onProgress({ stage: 'preparing', loaded, total })
    }
    request.onerror = () => fail('图片上传或任务创建时网络中断，请检查连接后重试')
    request.onabort = () => fail('上传已取消')
    request.ontimeout = () => fail('图片上传或任务创建超时，请重试')
    request.onload = () => {
      if (settled) return
      const payload = request.response
      if (request.status < 200 || request.status >= 300) {
        const detail = payload?.detail
        const message = typeof detail === 'string' ? detail : Array.isArray(detail)
          ? detail.map((item: { msg?: string }) => item.msg || '').filter(Boolean).join('；')
          : ''
        fail(message || `任务创建失败（HTTP ${request.status}），请重试`)
        return
      }
      if (!payload || typeof payload.batch_id !== 'string' || typeof payload.status_url !== 'string' || !Number.isInteger(payload.total) || payload.total < 1) {
        fail('服务器未返回有效的合成任务，请重试')
        return
      }
      settled = true
      signal?.removeEventListener('abort', abort)
      resolve(payload as BatchSubmission)
    }
    onProgress({ stage: 'uploading', loaded, total })
    if (signal?.aborted) { fail('上传已取消'); return }
    signal?.addEventListener('abort', abort, { once: true })
    request.send(body)
  })
}

// Reserve first so cancellation remains addressable while the multipart POST
// is still uploading or preparing and has not returned a batch ID yet.
export class CompositionTask {
  private upload = new AbortController()
  private batch: BatchSubmission | null = null
  private cancelPending: Promise<void> | null = null
  cancellationRequested = false

  constructor(private onCancelError: (message: string) => void) {}

  async begin(body: FormData, onProgress: (progress: SubmissionProgress) => void) {
    const response = await fetch('/api/batches/reserve', { method: 'POST', signal: AbortSignal.timeout(15_000) })
    if (!response.ok) throw new Error('任务创建失败，请重试')
    const reserved = await response.json() as BatchSubmission
    if (!/^[a-f0-9]{12}$/.test(reserved.batch_id) || reserved.status_url !== `/api/batches/${reserved.batch_id}/status`) throw new Error('任务创建失败，请重试')
    this.batch = reserved
    body.set('batch_id', reserved.batch_id)
    if (this.cancellationRequested) { this.cancel(); return reserved }
    try {
      return await submitComposition(body, onProgress, this.upload.signal)
    } catch (cause) {
      if (!this.cancellationRequested) throw cause
      this.cancel()
      return reserved
    }
  }

  cancel() {
    this.cancellationRequested = true
    this.upload.abort()
    if (!this.batch || this.cancelPending) return
    this.cancelPending = fetch(`/api/batches/${this.batch.batch_id}/cancel`, { method: 'POST', signal: AbortSignal.timeout(15_000) })
      .then(async (response) => {
        if (!response.ok) throw new Error()
      })
      .catch(() => this.onCancelError('取消请求未确认，请点击“取消任务”重试；任务可能仍在处理'))
      .finally(() => { this.cancelPending = null })
  }
}
