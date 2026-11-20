import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { build } from 'rolldown'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const require = createRequire(import.meta.url)
async function load(filename, jsx=false) {
  const result = await build({ input: fileURLToPath(new URL(`../src/${filename}`, import.meta.url)), write:false, platform:'node', external:['react','react/jsx-runtime'], output:{format:'esm'} })
  let code = result.output.find((item)=>item.type==='chunk').code
  if(jsx) code=code.replaceAll('"react/jsx-runtime"',JSON.stringify(pathToFileURL(require.resolve('react/jsx-runtime')).href))
    .replaceAll('"react"',JSON.stringify(pathToFileURL(require.resolve('react')).href))
  return import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
}
const { ConversionTask, conversionResponse, loadRecentConversion, saveRecentConversion, forgetRecentConversion }=await load('conversionRequest.ts')
const { PhotoConverter, FrameChoice, conversionAccept }=await load('PhotoConverter.tsx',true)

class FakeXHR {
  static current
  static count=0
  upload={}
  response={}
  status=200
  constructor(){FakeXHR.current=this;FakeXHR.count++}
  open(method,url){this.method=method;this.url=url}
  send(body){this.body=body}
  abort(){this.aborted=true;this.onabort?.()}
}
globalThis.XMLHttpRequest=FakeXHR
const id='123456abcdef9876'
const ok=(payload)=>new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}})

test('converter uses independent endpoints and preserves selected format and files',async()=>{
  const previous=globalThis.fetch
  globalThis.fetch=async(url)=>{assert.equal(url,'/api/converter/reserve');return ok({id})}
  try{
    const stages=[]
    const file=new File(['test'],'phone.heic',{type:'image/heic'})
    const task=new ConversionTask()
    const promise=task.upload([file],'png',(stage)=>stages.push(stage))
    while(!FakeXHR.current?.body)await new Promise(setImmediate)
    const request=FakeXHR.current
    assert.equal(request.url,'/api/converter/upload')
    assert.equal(request.body.get('output_format'),'png')
    assert.equal(request.body.get('job'),id)
    assert.equal(request.body.get('files').name,'phone.heic')
    request.upload.onprogress({loaded:50,total:100,lengthComputable:true})
    assert.deepEqual(stages.at(-1),{loaded:50,total:100,preparing:false})
    request.upload.onload()
    assert.equal(stages.at(-1).preparing,true)
    request.onload()
    assert.equal(await promise,id)
    const count=stages.length
    request.upload.onprogress({loaded:99,total:100,lengthComputable:true})
    assert.equal(stages.length,count)
  }finally{globalThis.fetch=previous;FakeXHR.current=null}
})

test('cancel during reservation skips upload and cancels the reserved converter task',async()=>{
  const previous=globalThis.fetch
  let release
  const urls=[]
  globalThis.fetch=(url)=>{urls.push(url);return url.endsWith('/reserve')?new Promise((resolve)=>{release=resolve}):Promise.resolve(ok({status:'cancelled'}))}
  try{
    const before=FakeXHR.count
    const task=new ConversionTask()
    const promise=task.upload([new File(['test'],'a.gif')],'jpeg',()=>{})
    await task.cancel()
    release(ok({id}))
    assert.equal(await promise,id)
    assert.equal(FakeXHR.count,before)
    assert.deepEqual(urls,['/api/converter/reserve',`/api/converter/${id}/cancel`])
  }finally{globalThis.fetch=previous}
})

test('upload cancellation aborts transport and cancellation failure can be retried',async()=>{
  const previous=globalThis.fetch
  let fail=false
  globalThis.fetch=async(url)=>url.endsWith('/reserve')?ok({id}):fail?new Response('unavailable',{status:503}):ok({status:'cancelled'})
  try{
    const task=new ConversionTask()
    const promise=task.upload([new File(['test'],'a.gif')],'jpeg',()=>{})
    while(!FakeXHR.current)await new Promise(setImmediate)
    await task.cancel()
    assert.equal(FakeXHR.current.aborted,true)
    assert.equal(await promise,id)
    fail=true
    await assert.rejects(task.cancel(),/503/)
    fail=false
    await task.cancel()
  }finally{globalThis.fetch=previous;FakeXHR.current=null}
})

test('HTML or validation failures are reported without pretending conversion completed',async()=>{
  await assert.rejects(conversionResponse(new Response('<html>bad gateway</html>',{status:502})),/502/)
  await assert.rejects(conversionResponse(new Response(JSON.stringify({detail:'磁盘不足'}),{status:507})),/磁盘不足/)
})

test('frame picker shows the selected frame index and one-based controls',()=>{
  const html=renderToStaticMarkup(createElement(FrameChoice,{job:id,source:{index:7,name:'animation.gif',frames:5,width:60,height:40},value:2,onChange:()=>{}}))
  assert.ok(html.includes(`/frame/7/2?v=0`))
  assert.ok(html.includes('value="3"'))
  assert.ok(html.includes('max="5"'))
  assert.ok(html.includes('当前选择第 3 帧/页'))
  assert.ok(html.includes('上一帧')&&html.includes('下一帧'))
})

test('converter has its own output controls and explains format limitations',()=>{
  const html=renderToStaticMarkup(createElement(PhotoConverter,{onClose:()=>{}}))
  assert.ok(html.includes('JPG / JPEG')&&html.includes('PNG'))
  assert.ok(html.includes('不缩放、不裁剪、不主动旋转'))
  assert.ok(html.includes('动图、多页图片可选择帧/页'))
  assert.ok(html.includes('不进入模板库或合成记录'))
  assert.ok(conversionAccept.includes('.heic')&&conversionAccept.includes('.gif'))
})

test('recent conversion remembers the scoped task and selected frame after reopening', () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const storage = new Map()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
  })
  try {
    saveRecentConversion({ id, frames: { 1: 2, 2: -1 } })
    const recent = loadRecentConversion()
    assert.deepEqual(recent, { id, frames: { 1: 2 } })
    assert.equal(new ConversionTask(recent.id).id, id)
    const html = renderToStaticMarkup(createElement(PhotoConverter, { onClose() {} }))
    assert.ok(html.includes('恢复最近一次转换'))
    forgetRecentConversion('0000000000000000')
    assert.equal(loadRecentConversion()?.id, id)
    forgetRecentConversion(id)
    assert.equal(loadRecentConversion(), null)
  } finally {
    if (previous) Object.defineProperty(globalThis, 'localStorage', previous)
    else delete globalThis.localStorage
  }
})
