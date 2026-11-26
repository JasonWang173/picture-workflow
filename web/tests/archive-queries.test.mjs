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

const newSession = name => import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}#${name}`)

function mockStorage(context) {
  const entries = new Map()
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value),
    removeItem: key => entries.delete(key),
  } })
  context.after(() => {
    if (original) Object.defineProperty(globalThis, 'localStorage', original)
    else delete globalThis.localStorage
  })
  return entries
}

test('reload shows the confirmed account directory while offline, isolates other viewers and honors legacy expiry', async (context) => {
  const entries = mockStorage(context)
  let respond
  const fetch = context.mock.method(globalThis, 'fetch', () => new Promise(resolve => { respond = resolve }))
  const key = 'view=batch_folders&section=history&scope=team&viewer=wzj'
  const folders = [{ folder:'2026-11-25-wzj（1）', day:'2026-11-25', count:2 }]
  const now = Date.now()
  let clock = now
  context.mock.method(Date, 'now', () => clock)
  const initial = await newSession('first')
  const first = initial.readArchiveQuery(key)
  respond(Response.json({ folders, expires_at: new Date(now + 120000).toISOString() }))
  await first
  assert.doesNotMatch(fetch.mock.calls[0].arguments[0], /viewer=/)
  clock += 61000
  const reopened = await newSession('reopened')
  const read = reopened.readArchiveQuery(key)
  assert.deepEqual(reopened.getArchiveQuerySnapshot(key).data.folders, folders)
  respond(new Response(null, { status: 503 }))
  await read
  assert.deepEqual(reopened.getArchiveQuerySnapshot(key).data.folders, folders)
  for (const otherKey of [key.replace('wzj', 'zs'), 'view=batch_folders&section=history&scope=browser']) {
    const other = reopened.readArchiveQuery(otherKey)
    assert.equal(reopened.getArchiveQuerySnapshot(otherKey).loaded, false)
    respond(new Response(null, { status: 503 }))
    await other
  }
  clock += 65000
  const later = await newSession('expired')
  const expired = later.readArchiveQuery(key)
  assert.equal(later.getArchiveQuerySnapshot(key).loaded, false)
  respond(new Response(null, { status: 401 }))
  await expired
  assert.equal(entries.size, 0)
})

test('prefetch shares one request with opening and a fresh workbench reload needs no directory request', async (context) => {
  const entries = mockStorage(context)
  let respond
  const fetch = context.mock.method(globalThis, 'fetch', () => new Promise(resolve => { respond = resolve }))
  const app = await newSession('prefetch')
  const key = app.folderQueryKey('history', 'wzj')
  const warm = app.warmArchiveHistory('wzj')
  assert.equal(app.readArchiveQuery(key), warm)
  const first = Date.now() + 3600000
  const folders = [{ username: 'zs', day: '2026-11-24', count: 1, expiry_offsets_ms: [first] }]
  respond(Response.json({ folders, expires_at: new Date(first).toISOString() }))
  await warm
  const next = await newSession('fresh')
  await next.warmArchiveHistory('wzj')
  await next.readArchiveQuery(key)
  assert.deepEqual(next.getArchiveQuerySnapshot(key).data.folders, folders)
  assert.equal(fetch.mock.callCount(), 1)
  next.invalidateArchiveQueries() // Download, restore, or trash with the panel closed.
  assert.equal(fetch.mock.callCount(), 2)
  assert.equal(entries.size, 1)
  assert.deepEqual(next.getArchiveQuerySnapshot(key).data.folders, folders)
  const pending = next.readArchiveQuery(key)
  next.clearArchiveQueries()
  respond(Response.json({ folders }))
  await pending
  assert.equal(entries.size, 0)
  assert.equal(next.getArchiveQuerySnapshot(key).loaded, false)
})

test('next-day offline search prunes individual expirations, retaining other users and batches during refresh', async (context) => {
  mockStorage(context)
  const start = Date.now(), day = 86400000
  let clock = start, respond
  context.mock.method(Date, 'now', () => clock)
  const fetch = context.mock.method(globalThis, 'fetch', () => new Promise(resolve => { respond = resolve }))
  const old = await newSession('monday')
  const key = old.folderQueryKey('history', 'wzj')
  const expires = start + day
  const folders = [
    { username: 'zs', day: '2026-11-24', count: 4, expiry_offsets_ms: [expires, 0, day, day] },
    { username: 'old', day: '2026-11-23', count: 1, expiry_offsets_ms: [expires] },
    { username: 'wzj', day: '2026-11-24', count: 1, expiry_offsets_ms: [start + 7 * day] },
  ]
  const first = old.warmArchiveHistory('wzj')
  respond(Response.json({ folders, expires_at: new Date(expires).toISOString() }))
  await first
  clock += day + 100
  const next = await newSession('next-day')
  const pending = next.warmArchiveHistory('wzj')
  assert.equal(next.readArchiveQuery(key), pending)
  const counts = app => app.getArchiveQuerySnapshot(key).data.folders.map(f => [f.username, f.count])
  assert.deepEqual(counts(next), [['zs', 2], ['wzj', 1]])
  clock += day
  assert.equal(next.readArchiveQuery(key), pending)
  assert.deepEqual(counts(next), [['zs', 1], ['wzj', 1]])
  respond(new Response(null, { status: 503 }))
  await pending
  assert.deepEqual(counts(next), [['zs', 1], ['wzj', 1]])
  const manual = next.readArchiveQuery(key, 'refresh')
  assert.match(fetch.mock.calls.at(-1).arguments[0], /refresh=true/)
  assert.deepEqual(counts(next), [['zs', 1], ['wzj', 1]])
  respond(new Response(null, { status: 503 }))
  await manual
  clock = start + 7 * day
  const expired = await newSession('next-week')
  assert.deepEqual(counts(expired), [])
})
