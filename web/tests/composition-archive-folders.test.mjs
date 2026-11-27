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
const { groupArchiveEntriesByDay, groupArchiveFolders, archiveBatchLabel } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)

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

test('account directories isolate dates and order existing batch numbers numerically despite recent additions', () => {
  const folder = (username, day, number, count, latest_at) => Object.freeze({
    username, day, count, latest_at, folder: `${day}-${username}（${number}）`,
  })
  const folders = Object.freeze([
    folder('zs', '2026-11-27', 10, 8, '2026-11-27T05:00:00Z'),
    folder('wl', '2026-11-27', 1, 3),
    folder('zs', '2026-11-27', 2, 4),
    folder('xx', '2026-11-27', 1, 2),
    folder('zs', '2026-11-26', 1, 1),
    folder('zs', '2026-11-27', 1, 5, '2026-11-27T09:00:00Z'),
  ])
  const accounts = groupArchiveFolders(folders)
  assert.deepEqual(accounts.map(a => a.key), ['wl', 'xx', 'zs'])
  const zs = accounts[2]
  assert.equal(zs.count, 18)
  assert.equal(zs.batches, 4)
  assert.deepEqual(zs.days.map(d => d.day), ['2026-11-27', '2026-11-26'])
  assert.equal(zs.days[0].count, 17)
  assert.deepEqual(zs.days[0].folders.map(archiveBatchLabel), ['第 1 批', '第 2 批', '第 10 批'])
})

test('older cached folders, untagged records and same-name batches remain reachable without mixing accounts', () => {
  const named = { day: '2026-11-27', folder: '2026-11-27-zs（1）', count: 1 }
  const folders = [
    { ...named, batch: 'bbbbbbbbbbbb' }, { ...named, batch: 'aaaaaaaaaaaa' },
    { day: '2026-11-26', count: 2 }, // A record created before account-based filenames.
    { ...named, username: 'wl', folder: '2026-11-27-wl（1）', count: 0 },
  ]
  const accounts = groupArchiveFolders(folders)
  assert.deepEqual(accounts.map(a => a.label), ['zs', '未标记账号'])
  assert.equal(accounts[0].batches, 2)
  assert.deepEqual(accounts[0].days[0].folders.map(f => f.batch), ['aaaaaaaaaaaa', 'bbbbbbbbbbbb'])
  assert.equal(accounts[1].count, 2)
  assert.equal(archiveBatchLabel(accounts[1].days[0].folders[0]), '历史记录')
  assert.deepEqual(groupArchiveFolders([]), [])
})
