import type { SubmissionProgress } from './compositionRequest'

export type BatchProgress = {
  done: number
  total: number
  status?: string
  templateIndex?: number
  templateCount?: number
  templateName?: string
}

export function progressStage(submission: SubmissionProgress | null) {
  return submission?.stage || 'composing'
}

function bytes(value: number) {
  return value >= 1024 * 1024 ? `${(value / (1024 * 1024)).toFixed(1)} MB` : `${Math.round(value / 1024)} KB`
}

export function ProcessingProgress({ submission, batch, templateOnly }: { submission: SubmissionProgress | null; batch: BatchProgress | null; templateOnly: boolean }) {
  const stage = progressStage(submission)
  const current = stage === 'uploading' ? 0 : stage === 'preparing' ? 1 : 2
  const percentage = stage === 'uploading' && submission?.total
    ? Math.min(100, Math.floor((submission.loaded / submission.total) * 100))
    : stage === 'composing' && batch?.total
      ? Math.min(100, Math.floor((batch.done / batch.total) * 100))
      : null
  const title = stage === 'uploading' ? `正在上传图片${percentage === null ? '' : ` ${percentage}%`}`
    : stage === 'preparing' ? '服务器准备任务'
      : batch?.status === 'done' ? '合成完成，正在读取结果'
        : batch?.status === 'queued' ? '任务已创建，等待开始合成'
          : `正在合成 ${batch?.done || 0} / ${batch?.total || 1} 张`
  const hint = stage === 'uploading' ? '正在发送所选图片，请保持页面打开'
    : stage === 'preparing' ? '图片已上传，服务器正在保存图片并创建任务'
      : batch?.status === 'queued' ? '正在等待可用的处理资源'
        : templateOnly ? '按模板顺序生成空白模板' : '每张模板处理完全部商品后，再开始下一张模板'
  return (
    <div className="batch-progress" role="status">
      <ol className="processing-steps" aria-label="处理阶段">
        {['上传图片', '准备任务', '合成图片'].map((label, index) => (
          <li key={label} className={index === current ? 'is-current' : index < current ? 'is-complete' : ''} aria-current={index === current ? 'step' : undefined}><span>{index < current ? '✓' : index + 1}</span>{label}</li>
        ))}
      </ol>
      <span className="batch-progress-icon" aria-hidden="true">↻</span>
      <strong>{title}</strong>
      <div className={`batch-progress-bar ${percentage === null ? 'is-indeterminate' : ''}`} role="progressbar" aria-label={stage === 'uploading' ? '图片上传进度' : stage === 'preparing' ? '服务器准备任务' : '合成进度'} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percentage ?? undefined}>
        <i style={percentage === null ? undefined : { width: `${percentage}%` }} />
      </div>
      {stage === 'uploading' && submission && <span className="upload-byte-count">已发送 {bytes(submission.loaded)}{submission.total === null ? '' : ` / ${bytes(submission.total)}`}</span>}
      {stage === 'composing' && batch?.templateIndex && <span>正在处理第 {batch.templateIndex} / {batch.templateCount} 个模板：{batch.templateName}</span>}
      <span>{hint}</span>
    </div>
  )
}
