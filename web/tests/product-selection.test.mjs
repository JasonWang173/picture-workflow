import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { build } from 'rolldown'

const bundle = await build({ input: fileURLToPath(new URL('../src/productSelection.ts', import.meta.url)), write: false, platform: 'node', output: { format: 'esm' } })
const { ProductSelection, fileDigest, imageDigest } = await import(`data:text/javascript;base64,${Buffer.from(bundle.output.find((item) => item.type === 'chunk').code).toString('base64')}`)
const file = (value, name = 'product.png') => new File([value], name, { type: 'image/png' })
const uniquePixels = (item) => item.text()

test('renamed copies deduplicate within a batch and across later additions', async () => {
  let decodes = 0
  const selection = new ProductSelection(fileDigest, async (item) => { decodes++; return item.text() })
  const first = file('same bytes', 'first.png')
  let result = await selection.append([first, file('same bytes', 'renamed.png')])
  assert.deepEqual(result.added, [first])
  assert.equal(result.duplicates, 1)
  result = await selection.append([file('same bytes', 'other-folder.png')])
  assert.equal(result.duplicates, 1)
  assert.equal(decodes, 1, 'exact copies should skip repeated image decoding')
})

test('same name and size with different content are retained', async () => {
  const selection = new ProductSelection(fileDigest, uniquePixels)
  const a = file('red!'), b = file('blue')
  assert.equal(a.size, b.size)
  const result = await selection.append([a, b])
  assert.deepEqual(result.added, [a, b])
  assert.equal(result.duplicates, 0)
})

test('different file bytes with exactly equal decoded pixels keep the first file', async () => {
  const selection = new ProductSelection(fileDigest, async () => '120x160:same-pixels')
  const a = file('PNG encoding A'), b = file('PNG encoding B with metadata')
  const result = await selection.append([a, b])
  assert.deepEqual(result.added, [a])
  assert.equal(result.duplicates, 1)
})

test('overlapping folder selections run in order without losing unique files', async () => {
  const selection = new ProductSelection(async (item) => { await new Promise((resolve) => setTimeout(resolve, 5)); return fileDigest(item) }, uniquePixels)
  const a = file('A'), b = file('B'), c = file('C')
  const first = selection.append([a, b])
  const second = selection.append([file('B', 'copy.png'), c])
  assert.equal(selection.pending, 2)
  const results = await Promise.all([first, second])
  assert.deepEqual(results.flatMap((item) => item.added), [a, b, c])
  assert.equal(results[1].duplicates, 1)
  assert.equal(selection.pending, 0)
})

test('clearing during a check cancels old results and allows the image again', async () => {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  const selection = new ProductSelection(async (item) => { await gate; return fileDigest(item) }, uniquePixels)
  const a = file('A')
  const oldVersion = selection.version
  const pending = selection.append([a])
  await new Promise((resolve) => setTimeout(resolve, 0))
  selection.clear()
  release()
  assert.equal((await pending).cancelled, true)
  assert.notEqual(selection.version, oldVersion)
  assert.deepEqual((await selection.append([a])).added, [a])
})

test('a failed image does not block later files or future additions', async () => {
  const selection = new ProductSelection(fileDigest, async (item) => {
    if (item.name === 'broken.png') throw new Error('cannot decode')
    return item.text()
  })
  const bad = file('broken', 'broken.png'), good = file('good')
  const result = await selection.append([bad, good])
  assert.deepEqual(result.added, [good])
  assert.deepEqual(result.failed, [bad])
  assert.equal((await selection.append([file('good', 'renamed.png')])).duplicates, 1)
})

test('full pixel hashing preserves one-pixel differences and releases bitmap memory', async () => {
  const originalBitmap = globalThis.createImageBitmap
  const originalDocument = globalThis.document
  const canvases = []
  let closed = 0
  globalThis.createImageBitmap = async (item, options) => {
    assert.equal(options.imageOrientation, 'from-image')
    return { width: 2, height: 1, value: await item.text(), close: () => { closed++ } }
  }
  globalThis.document = { createElement: () => {
    let value
    const canvas = { width: 0, height: 0, getContext: () => ({
      drawImage: (bitmap) => { value = bitmap.value },
      getImageData: (x,y,w,h) => {
        assert.deepEqual([w,h], [2,1])
        return { data: new Uint8ClampedArray([255,0,0,255,0,0,value === 'changed' ? 1 : 0,255]) }
      },
    }) }
    canvases.push(canvas)
    return canvas
  } }
  try {
    const first = await imageDigest(file('original'))
    assert.equal(first, await imageDigest(file('same pixels, different encoding')))
    assert.notEqual(first, await imageDigest(file('changed')))
    assert.equal(closed, 3)
    assert.ok(canvases.every((canvas) => canvas.width === 1 && canvas.height === 1))
  } finally {
    globalThis.createImageBitmap = originalBitmap
    globalThis.document = originalDocument
  }
})
