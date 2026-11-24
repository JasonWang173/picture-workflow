export function composeBlocker(input: {
  templateOnly: boolean; templateCount: number; invalidTemplate: boolean
  productCount: number; usableProductCount: number; checkingProducts: boolean
  composing: boolean; libraryBusy: boolean; resultAction: string
}) {
  if (input.composing) return '当前任务结束或取消后，可以开始下一批。'
  if (input.libraryBusy) return '正在处理模板库，请稍候。'
  if (input.resultAction) return input.resultAction === 'zip' ? '正在打包下载，请稍候。' : '正在处理合成结果，请稍候。'
  if (input.checkingProducts) return '正在检查重复商品图，检查完成后即可继续。'
  if (input.invalidTemplate) return '请移除不支持的模板图，或换成 JPG、PNG、WebP。'
  if (!input.templateCount && !input.templateOnly && !input.productCount) return '还需添加至少 1 张商品图和 1 张模板图。'
  if (!input.templateCount) return '还需添加至少 1 张模板图。'
  if (!input.templateOnly && !input.usableProductCount) {
    return input.productCount ? '请添加 JPG、PNG 或 WebP 格式的商品图。' : '还需添加至少 1 张商品图。'
  }
  return ''
}

export function firstOutputFilename(day: string, username: string | null, size: string, total: number, productName: string | null) {
  const date = day.replace(/-/g, '')
  if (username) {
    const [width, height] = size.split('x').map(Number)
    const orientation = width === height ? '方版' : width > height ? '横板' : '竖版'
    return `${date}-${username}-${orientation}-（1）.jpg`
  }
  // Match the guest filenames in server._composition_filename, including its Unicode character limit.
  const basename = productName === null ? '空白模板' : productName.split(/[\\/]/).pop() || ''
  const extension = basename.lastIndexOf('.')
  const stem = (extension > 0 ? basename.slice(0, extension) : basename).trim()
  const safe = stem.replace(/[<>:"/\\|?*\u0000-\u001f\u007f]+/g, '_').replace(/^[ ._]+|[ ._]+$/g, '')
  const dish = Array.from(safe).slice(0, 56).join('').replace(/[ ._]+$/g, '') || '美食'
  const sequence = '1'.padStart(Math.max(3, String(total).length), '0')
  return `${date}-${sequence}-${dish}.jpg`
}
