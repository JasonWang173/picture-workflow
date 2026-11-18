import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { build } from 'rolldown'

const bundle = await build({
  input: fileURLToPath(new URL('../src/compositionArchiveFolders.ts', import.meta.url)),
  write: false,
  platform: 'node',
  output: { format: 'esm' },
})
const source = bundle.output.find((item) => item.type === 'chunk').code
const { groupArchiveEntriesByDay } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)

test('groups history into newest-first Shanghai calendar-day folders', () => {
  const entries = [
    { id: 'late-17', saved_at: '2026-11-17T15:59:00Z' },
    { id: 'early-18', saved_at: '2026-11-17T16:01:00Z' },
    { id: 'late-18', saved_at: '2026-11-18T03:30:00Z' },
    { id: 'unknown', saved_at: 'not-a-date' },
  ]

  const groups = groupArchiveEntriesByDay(entries)
  assert.deepEqual(groups.map((group) => group.key), ['2026-11-18', '2026-11-17', 'unknown'])
  assert.equal(groups[0].label, '2026年11月18日')
  assert.deepEqual(groups[0].items.map((item) => item.id), ['late-18', 'early-18'])
  assert.deepEqual(groups[1].items.map((item) => item.id), ['late-17'])
})
