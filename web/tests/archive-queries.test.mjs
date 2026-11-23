import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { build } from 'rolldown'

const require = createRequire(import.meta.url)
const bundle = await build({
  input: fileURLToPath(new URL('../src/archiveQueries.ts', import.meta.url)),
  write: false, platform: 'node', external: ['react'], output: { format: 'esm' },
})
const source = bundle.output.find((item) => item.type === 'chunk').code
  .replaceAll('"react"', JSON.stringify(pathToFileURL(require.resolve('react')).href))
const { readArchiveQuery, getArchiveQuerySnapshot, invalidateArchiveQueries, clearArchiveQueries } =
  await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)

test('pages append without duplicates, reuse cache and reject a pre-mutation refresh', async (context) => {
  const key = 'view=page&section=history&day=2026-11-18'
  let resolve
  const fetch = context.mock.method(globalThis, 'fetch', () => new Promise((done) => { resolve = done }))
  const first = readArchiveQuery(key)
  assert.equal(readArchiveQuery(key), first)
  resolve(Response.json({ items: [{ id: 'a' }, { id: 'b' }], next_cursor: 'next', total: 3 }))
  await first
  await readArchiveQuery(key)
  assert.equal(fetch.mock.callCount(), 1)
  const more = readArchiveQuery(key, false, true)
  assert.match(fetch.mock.calls[1].arguments[0], /cursor=next/)
  resolve(Response.json({ items: [{ id: 'b' }, { id: 'c' }], next_cursor: null, total: 3 }))
  await more
  assert.deepEqual(getArchiveQuerySnapshot(key).data.items.map((item) => item.id), ['a', 'b', 'c'])

  const oldRead = readArchiveQuery(key, true)
  invalidateArchiveQueries()
  resolve(Response.json({ items: [{ id: 'outdated' }] }))
  await oldRead
  assert.equal(getArchiveQuerySnapshot(key).data.items.length, 3)
  const retry = readArchiveQuery(key)
  resolve(new Response(null, { status: 503 }))
  await retry
  assert.equal(getArchiveQuerySnapshot(key).data.items.length, 3)
  assert.ok(getArchiveQuerySnapshot(key).error)
})

test('changing sessions or losing access clears the cached team folder', async (context) => {
  const key = 'view=page&scope=team&folder=sample'
  let respond
  context.mock.method(globalThis, 'fetch', () => new Promise(resolve => { respond = resolve }))
  const first = readArchiveQuery(key)
  respond(Response.json({ items: [{ id: 'shared' }] }))
  await first
  const refresh = readArchiveQuery(key, true)
  clearArchiveQueries()
  respond(Response.json({ items: [{ id: 'from-previous-session' }] }))
  await refresh
  assert.deepEqual(getArchiveQuerySnapshot(key).data, {})

  const signedIn = readArchiveQuery(key)
  respond(Response.json({ items: [{ id: 'shared' }] }))
  await signedIn
  const expired = readArchiveQuery(key, true)
  respond(Response.json({ detail: '请重新登录' }, { status: 401 }))
  await expired
  assert.deepEqual(getArchiveQuerySnapshot(key).data, {})
  assert.match(getArchiveQuerySnapshot(key).error, /重新登录/)
})
