export type UploadProgress = { loaded: number; total: number | null; preparing: boolean }

export async function conversionResponse(response: Response) {
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : `转换请求失败（${response.status}）`)
  return payload
}

export class ConversionTask {
  id: string | null = null
  requestedCancel = false
  private uploadRequest: XMLHttpRequest | null = null
  private pendingCancel: Promise<void> | null = null

  async upload(files: File[], outputFormat: 'jpeg' | 'png', onProgress: (value: UploadProgress) => void) {
    const reserve = await conversionResponse(await fetch('/api/converter/reserve', { method: 'POST', signal: AbortSignal.timeout(15_000) }))
    if (!/^[a-f0-9]{16}$/.test(reserve?.id)) throw new Error('服务器未返回有效转换任务')
    this.id = reserve.id
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
