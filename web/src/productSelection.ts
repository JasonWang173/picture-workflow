// Full-resolution equality only: no thumbnail/perceptual similarity threshold
// that could incorrectly discard a different product or a small visual change.
export async function fileDigest(file: File): Promise<string> {
  return digest(await file.arrayBuffer())
}

async function digest(buffer: ArrayBuffer): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export async function imageDigest(file: File): Promise<string> {
  let source: ImageBitmap | HTMLImageElement
  let url: string | undefined
  if (typeof createImageBitmap === 'function') {
    source = await createImageBitmap(file, { imageOrientation: 'from-image' })
  } else {
    url = URL.createObjectURL(file)
    const image = new Image()
    image.src = url
    try {
      await image.decode()
    } catch (cause) {
      URL.revokeObjectURL(url)
      throw cause
    }
    source = image
  }
  const canvas = document.createElement('canvas')
  try {
    const width = 'naturalWidth' in source ? source.naturalWidth : source.width
    const height = 'naturalHeight' in source ? source.naturalHeight : source.height
    if (!width || !height || width * height > 24_000_000) throw new Error('图片像素数不能超过 2400 万')
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (!context) throw new Error('无法读取图片内容')
    context.drawImage(source, 0, 0)
    const pixels = context.getImageData(0, 0, width, height).data
    return `${width}x${height}:${await digest(pixels.buffer as ArrayBuffer)}`
  } finally {
    if ('close' in source) source.close()
    if (url) URL.revokeObjectURL(url)
    canvas.width = canvas.height = 1
  }
}

export type SelectionResult = { added: File[]; duplicates: number; failed: File[]; cancelled: boolean }

export class ProductSelection {
  private binary = new Set<string>()
  private pixels = new Set<string>()
  private generation = 0
  private queue: Promise<unknown> = Promise.resolve()
  private count = 0

  constructor(private readFile = fileDigest, private readImage = imageDigest) {}

  get pending() { return this.count }
  get version() { return this.generation }

  clear() {
    this.generation += 1
    this.binary.clear()
    this.pixels.clear()
  }

  append(files: File[], onProgress?: (checked: number, total: number) => void): Promise<SelectionResult> {
    const generation = this.generation
    this.count += 1
    const task = this.queue.then(async () => {
      const result: SelectionResult = { added: [], duplicates: 0, failed: [], cancelled: false }
      const active = () => generation === this.generation
      if (active()) onProgress?.(0, files.length)
      for (let index = 0; index < files.length; index += 1) {
        if (!active()) return { ...result, added: [], cancelled: true }
        const file = files[index]
        try {
          const binary = await this.readFile(file)
          if (!active()) return { ...result, added: [], cancelled: true }
          if (this.binary.has(binary)) {
            result.duplicates += 1
          } else {
            const pixels = await this.readImage(file)
            if (!active()) return { ...result, added: [], cancelled: true }
            if (this.pixels.has(pixels)) result.duplicates += 1
            else {
              this.pixels.add(pixels)
              result.added.push(file)
            }
            this.binary.add(binary)
          }
        } catch {
          if (!active()) return { ...result, added: [], cancelled: true }
          result.failed.push(file)
        }
        onProgress?.(index + 1, files.length)
      }
      return result
    }).finally(() => { this.count -= 1 })
    // Keep later selections running even if a prior selection fails.
    this.queue = task.catch(() => {})
    return task
  }
}
