import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { build } from 'rolldown'

const require = createRequire(import.meta.url)
const bundle = await build({
  input: fileURLToPath(new URL('../src/compositionArchiveStore.ts', import.meta.url)),
  write: false, platform: 'node', external: ['react'], output: { format: 'esm' },
})
const source = bundle.output.find((item) => item.type === 'chunk').code
  .replaceAll('"react"', JSON.stringify(pathToFileURL(require.resolve('react')).href))
const { getArchiveSnapshot, readCompositionArchive, updateArchiveRecords } =
  await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)

test('reopening uses the list cache and an in-flight refresh preserves newer changes', async (context) => {
  let now = Date.parse('2026-11-13T12:00:00Z')
  context.mock.method(Date, 'now', () => now)
  const active = { id: '1', section: 'history', saved_at: new Date(now).toISOString(), expires_at: new Date(now + 120_000).toISOString() }
  const expired = { ...active, id: 'expired', expires_at: new Date(now - 1).toISOString() }
  let resolveRead
  const fetch = context.mock.method(globalThis, 'fetch', () => new Promise((resolve) => { resolveRead = resolve }))

  const first = readCompositionArchive()
  assert.equal(readCompositionArchive(), first)
  resolveRead(Response.json({ items: [active, expired] }))
  await first
  assert.deepEqual(getArchiveSnapshot().items, [active])
  await readCompositionArchive()
  assert.equal(fetch.mock.callCount(), 1)

  now += 61_000
  const refresh = readCompositionArchive()
  assert.equal(getArchiveSnapshot().loading, true)
  assert.deepEqual(getArchiveSnapshot().items, [active])
  const moved = { ...active, section: 'trash' }
  const downloaded = { ...active, id: '2' }
  updateArchiveRecords([moved, downloaded])
  resolveRead(Response.json({ items: [active] }))
  await refresh
  assert.deepEqual(getArchiveSnapshot().items, [moved, downloaded])
  assert.equal(fetch.mock.callCount(), 2)

  const forced = readCompositionArchive(true)
  resolveRead(new Response(null, { status: 503 }))
  await forced
  assert.deepEqual(getArchiveSnapshot().items, [moved, downloaded])
  assert.ok(getArchiveSnapshot().error)
  now += 60_000
  const afterExpiry = readCompositionArchive()
  assert.deepEqual(getArchiveSnapshot().items, [])
  resolveRead(Response.json({ items: [] }))
  await afterExpiry
})
