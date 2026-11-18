import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { build } from 'rolldown'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const require = createRequire(import.meta.url)
async function loadSource(filename, jsx = false) {
  const result = await build({ input: fileURLToPath(new URL(`../src/${filename}`, import.meta.url)), write: false, platform: 'node', external: ['react', 'react/jsx-runtime'], output: { format: 'esm' } })
  let output = result.output.find((item) => item.type === 'chunk').code
  if (jsx) output = output.replaceAll('"react/jsx-runtime"', JSON.stringify(pathToFileURL(require.resolve('react/jsx-runtime')).href))
  return import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`)
}
const { submitComposition, CompositionTask } = await loadSource('compositionRequest.ts')
const { ProcessingProgress } = await loadSource('ProcessingProgress.tsx', true)

class FakeRequest {
  static current
  upload = {}
  status = 200
  response = { batch_id: '123456abcdef', total: 4, status_url: '/api/batches/123456abcdef/status' }
  constructor() { FakeRequest.current = this }
  open(method, url) { this.method = method; this.url = url }
  send(body) { this.body = body }
  abort() { this.aborted = true; this.onabort?.() }
}
globalThis.XMLHttpRequest = FakeRequest

test('upload bytes, waiting for server, then task response are distinct phases', async () => {
  const stages = []
  const promise = submitComposition(new FormData(), (progress) => stages.push(progress))
  const request = FakeRequest.current
  assert.equal(request.method, 'POST')
  assert.equal(request.url, '/api/compose-batch')
  assert.deepEqual(stages[0], { stage: 'uploading', loaded: 0, total: null })
  request.upload.onprogress({ loaded: 50, total: 100, lengthComputable: true })
  assert.deepEqual(stages.at(-1), { stage: 'uploading', loaded: 50, total: 100 })
  request.upload.onload()
  assert.deepEqual(stages.at(-1), { stage: 'preparing', loaded: 100, total: 100 })
  request.upload.onprogress({ loaded: 100, total: 100, lengthComputable: true })
  assert.equal(stages.at(-1).stage, 'preparing')
  request.onload()
  assert.equal((await promise).total, 4)
})

test('unknown transfer size stays unknown and does not invent a percentage', async () => {
  const stages = []
  const promise = submitComposition(new FormData(), (progress) => stages.push(progress))
  const request = FakeRequest.current
  request.upload.onprogress({ loaded: 100, total: 0, lengthComputable: false })
  assert.equal(stages.at(-1).total, null)
  request.onload()
  await promise
})

for (const event of ['onerror', 'onabort', 'ontimeout']) {
  test(`${event} terminates submission without stale progress updates`, async () => {
    const stages = []
    const promise = submitComposition(new FormData(), (progress) => stages.push(progress))
    const request = FakeRequest.current
    request[event]()
    await assert.rejects(promise)
    request.upload.onload()
    assert.equal(stages.length, 1)
  })
}

test('server validation errors and HTML error pages reject cleanly', async () => {
  for (const [status, response, message] of [[400, { detail: '图片格式不正确' }, /图片格式不正确/], [413, null, /HTTP 413/], [200, null, /有效的合成任务/]]) {
    const promise = submitComposition(new FormData(), () => {})
    Object.assign(FakeRequest.current, { status, response })
    FakeRequest.current.onload()
    await assert.rejects(promise, message)
  }
})

test('visible progress uses real transfer bytes and completed image counts', () => {
  const upload = renderToStaticMarkup(createElement(ProcessingProgress, { submission: { stage: 'uploading', loaded: 50, total: 100 }, batch: null, templateOnly: false }))
  assert.match(upload, /正在上传图片 50%/)
  assert.match(upload, /aria-valuenow="50"/)
  const preparing = renderToStaticMarkup(createElement(ProcessingProgress, { submission: { stage: 'preparing', loaded: 100, total: 100 }, batch: null, templateOnly: false }))
  assert.match(preparing, /服务器准备任务/)
  assert.doesNotMatch(preparing, /aria-valuenow/)
  const compose = renderToStaticMarkup(createElement(ProcessingProgress, { submission: null, batch: { done: 1, total: 4, status: 'processing' }, templateOnly: false }))
  assert.match(compose, /正在合成 1 \/ 4 张/)
  assert.match(compose, /aria-valuenow="25"/)
})

test('abort signal stops the upload and ignores a late task response', async () => {
  const controller = new AbortController()
  const stages = []
  const promise = submitComposition(new FormData(), (progress) => stages.push(progress), controller.signal)
  const request = FakeRequest.current
  controller.abort()
  await assert.rejects(promise, /取消/)
  assert.equal(request.aborted, true)
  request.onload()
  request.upload.onload()
  assert.equal(stages.length, 1)
})

test('cancelling during reservation never starts an upload and cancels the known batch', async (context) => {
  let resolveReserve
  const calls = []
  context.mock.method(globalThis, 'fetch', (url) => {
    calls.push(url)
    if (url.endsWith('/reserve')) return new Promise((resolve) => { resolveReserve = resolve })
    return Promise.resolve(Response.json({ status: 'cancelled' }))
  })
  const task = new CompositionTask(() => assert.fail('unexpected cancel error'))
  FakeRequest.current = null
  const starting = task.begin(new FormData(), () => {})
  task.cancel()
  resolveReserve(Response.json({ batch_id: '123456abcdef', total: 0, status_url: '/api/batches/123456abcdef/status' }))
  await starting
  assert.equal(FakeRequest.current, null)
  assert.deepEqual(calls, ['/api/batches/reserve', '/api/batches/123456abcdef/cancel'])
})

test('cancellation in server preparation aborts XHR and reports an unconfirmed cancel for retry', async (context) => {
  const errors = []
  const calls = []
  context.mock.method(globalThis, 'fetch', (url) => {
    calls.push(url)
    return Promise.resolve(url.endsWith('/reserve')
      ? Response.json({ batch_id: '123456abcdef', total: 0, status_url: '/api/batches/123456abcdef/status' })
      : new Response(null, { status: 503 }))
  })
  const task = new CompositionTask((error) => errors.push(error))
  const starting = task.begin(new FormData(), () => {})
  await new Promise((resolve) => setImmediate(resolve))
  FakeRequest.current.upload.onload()
  task.cancel()
  await starting
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(FakeRequest.current.aborted, true)
  assert.match(errors[0], /未确认/)
  task.cancel()
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(calls.filter((url) => url.endsWith('/cancel')).length, 2)
})

test('processing view exposes cancellation and prevents duplicate clicks while cancelling', () => {
  const props = { submission: null, batch: { done: 1, total: 4 }, templateOnly: false, onCancel: () => {} }
  assert.match(renderToStaticMarkup(createElement(ProcessingProgress, props)), />取消任务</)
  const cancelling = renderToStaticMarkup(createElement(ProcessingProgress, { ...props, cancelling: true }))
  assert.match(cancelling, /正在取消任务/)
  assert.match(cancelling, /disabled=""/)
})
