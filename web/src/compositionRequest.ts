export type SubmissionProgress = {
  stage: 'uploading' | 'preparing'
  loaded: number
  total: number | null
}

export type BatchSubmission = { batch_id: string; total: number; status_url: string }

// Fetch does not expose upload progress. XHR reports bytes actually sent by
// the browser, separately from waiting for the server to create the task.
export function submitComposition(body: FormData, onProgress: (progress: SubmissionProgress) => void): Promise<BatchSubmission> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    let loaded = 0
    let total: number | null = null
    let uploaded = false
    let settled = false
    const fail = (message: string) => {
      if (settled) return
      settled = true
      reject(new Error(message))
    }
    request.open('POST', '/api/compose-batch')
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
      resolve(payload as BatchSubmission)
    }
    onProgress({ stage: 'uploading', loaded, total })
    request.send(body)
  })
}
