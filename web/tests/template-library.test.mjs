import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { build } from 'rolldown'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const require = createRequire(import.meta.url)
const result = await build({
  input: fileURLToPath(new URL('../src/App.tsx', import.meta.url)),
  write: false,
  platform: 'node',
  external: ['react', 'react/jsx-runtime'],
  output: { format: 'esm' },
})
let source = result.output.find((item) => item.type === 'chunk').code
source = source
  .replaceAll('"react"', JSON.stringify(pathToFileURL(require.resolve('react')).href))
  .replaceAll('"react/jsx-runtime"', JSON.stringify(pathToFileURL(require.resolve('react/jsx-runtime')).href))
const { TemplateLibrary, TemplatePickerModal } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)

const templates = [
  { id: 'available', name: '可选模板', url: '/template-a.jpg' },
  { id: 'chosen', name: '已选模板', url: '/template-b.jpg' },
]
const noop = () => {}

test('template library separates image preview from the selection control', () => {
  const markup = renderToStaticMarkup(createElement(TemplateLibrary, {
    templates,
    selectedIds: ['chosen'],
    loading: false,
    busy: false,
    notice: '',
    onUpload: noop,
    onSelect: noop,
    onPreview: noop,
    onDelete: noop,
  }))
  assert.match(markup, /aria-label="预览模板 可选模板"/)
  assert.match(markup, /aria-label="选择模板 可选模板"/)
  assert.match(markup, /aria-label="模板 已选模板 已在队列中"/)
  assert.match(markup, /点图预览 · 点圆圈选择/)
})

test('multi-select picker keeps preview and checkbox actions independently available', () => {
  const markup = renderToStaticMarkup(createElement(TemplatePickerModal, {
    templates,
    selectedIds: [],
    loading: false,
    busy: false,
    notice: '',
    error: '',
    onPick: async () => true,
    onPreview: noop,
    onUpload: noop,
    onClose: noop,
  }))
  assert.match(markup, /aria-label="预览模板 可选模板"/)
  assert.match(markup, /aria-label="选择模板 可选模板"/)
  assert.match(markup, /aria-pressed="false"/)
  assert.match(markup, />添加所选模板</)
})
