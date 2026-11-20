"""Independent photo-converter jobs; no poster or OSS archive mutations."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .cancellation import CompositionCancelled, cancellation_scope
from .execution import submit_composition, worker_count
from .image_conversion import capabilities, convert_image, estimate_conversion_memory, inspect_image, preview_frame, result_thumbnail
from .storage_guard import StorageFull, ensure_space

LOG = logging.getLogger(__name__)
FINISHED = {'done', 'failed', 'cancelled'}


def _inspect(source, marker):
    with cancellation_scope(marker):
        return inspect_image(source)


def _preview(source, frame, marker):
    with cancellation_scope(marker):
        return preview_frame(source, frame)


def _convert(source, destination, options, frame, marker):
    try:
        with cancellation_scope(marker):
            return convert_image(source, destination, options['format'], frame=frame)
    except BaseException:
        Path(destination).unlink(missing_ok=True)
        raise


class ConversionJobs:
    def __init__(self, root, get_owner, save_upload, *, max_files=1000, max_file_mb=200, max_batch_bytes=8192*1024**2):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.get_owner, self.save_upload = get_owner, save_upload
        self.max_files, self.max_file_mb, self.max_batch_bytes = max_files, max_file_mb, max_batch_bytes
        self.locks = [Lock() for _ in range(64)]
        self.thumbnail_locks = [Lock() for _ in range(64)]
        self.router = self._routes()

    def lock(self, job):
        return self.locks[hash(job) % len(self.locks)]

    def directory(self, job):
        if not re.fullmatch(r'[0-9a-f]{16}', job):
            raise HTTPException(status_code=404, detail='转换任务不存在')
        path = self.root/job
        if not path.is_dir():
            raise HTTPException(status_code=404, detail='转换任务不存在或已过期')
        return path

    def read(self, job):
        path = self.directory(job)/'state.json'
        for attempt in range(6):
            try:
                return json.loads(path.read_text('utf-8'))
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail='转换任务不存在或已过期') from None
            except PermissionError:
                # Windows can briefly deny reads while a status update replaces the file.
                if attempt == 5:
                    raise
                time.sleep(.02)

    def owned(self, job, request):
        state = self.read(job)
        if not state.get('owner') or state['owner'] != self.get_owner(request):
            raise HTTPException(status_code=404, detail='转换任务不存在或已过期')
        return state

    def write(self, job, state):
        path = self.directory(job)/'state.json'
        temporary = path.with_name(uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        for attempt in range(10):
            try:
                temporary.replace(path)
                return
            except OSError:
                if attempt == 9:
                    temporary.unlink(missing_ok=True)
                    raise
                time.sleep(.02)

    def check_cancel(self, job):
        if (self.directory(job)/'cancel.requested').exists():
            raise CompositionCancelled('转换已取消')

    def finish(self, job, state, status, error=None):
        state.update(status=status, error=error, finished_at=datetime.now(timezone.utc).isoformat())
        with self.lock(job):
            # A frame preview can still be reading inputs when conversion or
            # cancellation finishes. Its finally block performs deferred cleanup.
            if not any(self.directory(job).glob('preview-*.lease')):
                shutil.rmtree(self.directory(job)/'inputs', ignore_errors=True)
            if (self.directory(job)/'cancel.requested').exists():
                state.update(status='cancelled', error=None)
            state['images'].sort(key=lambda value: value['id'])
            self.write(job, state)

    def inspect(self, job):
        state = self.read(job)
        state.update(status='inspecting', done=0, inputs=[])
        try:
            self.check_cancel(job)
            self.write(job, state)
            manifest = json.loads((self.directory(job)/'manifest.json').read_text('utf-8'))
            last_write = time.monotonic()
            for item in manifest['items']:
                self.check_cancel(job)
                path = self.directory(job)/'inputs'/item['file']
                info = {'index': item['index'], 'name': item['name']}
                try:
                    info.update(submit_composition(_inspect, str(path), str(self.directory(job)/'cancel.requested'),
                                                   estimated_bytes=64*1024**2, cancel_check=lambda: self.check_cancel(job)).result())
                except CompositionCancelled:
                    raise
                except Exception as exc:
                    info['error'] = str(exc) or '无法识别图片'
                state['inputs'].append(info)
                state['done'] += 1
                if time.monotonic()-last_write >= .25:
                    self.write(job, state)
                    last_write = time.monotonic()
            with self.lock(job):
                self.check_cancel(job)
                state['status'] = 'selecting'
                self.write(job, state)
        except CompositionCancelled:
            self.finish(job, state, 'cancelled')
        except Exception as exc:
            LOG.exception('图片格式识别失败')
            self.finish(job, state, 'failed', str(exc) or '识别图片失败，请重试')

    def run(self, job):
        state = self.read(job)
        state.update(status='processing', done=0, images=[], skipped=[])
        directory = self.directory(job)
        manifest = json.loads((directory/'manifest.json').read_text('utf-8'))
        futures = {}
        terminal, error = 'done', None
        last_write = 0
        def consume(future, item):
            if future.cancelled():
                return
            try:
                metadata = future.result()
            except CompositionCancelled:
                return
            except Exception as exc:
                state['skipped'].append({'index': item['index'], 'name': item['name'], 'error': str(exc) or '转换失败'})
            else:
                identifier = f"{item['index']:09d}"
                state['images'].append({'id': identifier, 'name': item['output'], 'input_name': item['name'], **metadata,
                                        'url': f'/api/converter/{job}/images/{identifier}',
                                        'download_url': f'/api/converter/{job}/images/{identifier}/download'})
            state['done'] += 1
        try:
            self.check_cancel(job)
            self.write(job, state)
            entries = iter(manifest['items'])
            def submit_next():
                self.check_cancel(job)
                item = next(entries, None)
                if item is None:
                    return
                source = directory/'inputs'/item['file']
                output = directory/'outputs'/item['output']
                ensure_space(directory, 1024**2)
                future = submit_composition(_convert, str(source), str(output), manifest['options'], item['frame'],
                                            str(directory/'cancel.requested'), estimated_bytes=estimate_conversion_memory(source),
                                            cancel_check=lambda: self.check_cancel(job))
                futures[future] = item
            for _ in range(min(worker_count(), len(manifest['items']))):
                submit_next()
            while futures:
                self.check_cancel(job)
                completed, _ = wait(futures, timeout=.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    consume(future, futures.pop(future))
                    if time.monotonic()-last_write > .25:
                        self.write(job, state)
                        last_write = time.monotonic()
                    submit_next()
            if not state['images']:
                terminal, error = 'failed', '没有图片转换成功，请检查图片格式或文件内容'
        except CompositionCancelled:
            terminal = 'cancelled'
        except Exception as exc:
            LOG.exception('图片格式转换失败')
            terminal, error = 'failed', str(exc) or '转换失败，请重试'
        finally:
            for future in futures:
                if terminal == 'cancelled':
                    future.cancel()
            if futures:
                wait(futures)
                for future, item in futures.items():
                    consume(future, item)
            self.finish(job, state, terminal, error)

    def purge(self, interrupted=False):
        now = datetime.now(timezone.utc)
        for path in self.root.glob('*/state.json'):
            job = path.parent.name
            if not re.fullmatch(r'[0-9a-f]{16}', job) or path.parent.resolve().parent != self.root.resolve():
                continue
            try:
                with self.lock(job):
                    state = self.read(job)
                    leases = list(path.parent.glob('preview-*.lease'))
                    if interrupted:
                        for lease in leases:
                            lease.unlink(missing_ok=True)
                        if state['status'] in FINISHED:
                            shutil.rmtree(path.parent/'inputs', ignore_errors=True)
                    elif leases:
                        continue  # Do not remove files still read by a preview.
                    if interrupted and state['status'] in {'uploading', 'inspecting', 'queued', 'processing', 'cancelling'}:
                        state.update(status='failed', error='服务重启，转换任务已中断，请重新上传', finished_at=now.isoformat())
                        shutil.rmtree(path.parent/'inputs', ignore_errors=True)
                        self.write(job, state)
                    hours = 168 if state['status'] in FINISHED else 24 if state['status'] == 'selecting' else 1 if state['status'] == 'reserved' else None
                    if hours is not None and datetime.fromisoformat(state.get('finished_at') or state['created_at'])+timedelta(hours=hours) <= now:
                        shutil.rmtree(path.parent)
                        continue
                    for archive in path.parent.glob('download-*.zip'):
                        if now.timestamp()-archive.stat().st_mtime > 3600:
                            archive.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError):
                LOG.warning('转换任务清理失败', exc_info=True)

    async def cleanup_loop(self):
        await run_in_threadpool(self.purge, True)
        while True:
            await asyncio.sleep(900)
            await run_in_threadpool(self.purge)

    def _routes(self):
        router = APIRouter(prefix='/api/converter', tags=['照片格式转换器'])

        @router.get('/capabilities')
        def formats():
            return {**capabilities(), 'max_files': self.max_files, 'max_file_mb': self.max_file_mb,
                    'max_batch_mb': self.max_batch_bytes//1024**2}

        @router.post('/reserve')
        def reserve(request: Request):
            job = uuid.uuid4().hex[:16]
            (self.root/job).mkdir()
            response = JSONResponse({'id': job, 'status_url': f'/api/converter/{job}'})
            self.write(job, {'owner': self.get_owner(request, response), 'status': 'reserved', 'total': 0, 'done': 0,
                             'images': [], 'inputs': [], 'skipped': [], 'created_at': datetime.now(timezone.utc).isoformat()})
            return response

        @router.post('/upload')
        async def upload(request: Request, background_tasks: BackgroundTasks,
                         job: Annotated[str, Form()], files: Annotated[list[UploadFile], File()],
                         output_format: Annotated[str, Form()] = 'jpeg'):
            if output_format not in {'jpeg', 'png'}:
                raise HTTPException(status_code=400, detail='输出格式只能选择 JPG 或 PNG')
            if not 1 <= len(files) <= self.max_files or any(not file.filename for file in files):
                raise HTTPException(status_code=413, detail=f'每批请选择 1 至 {self.max_files} 张图片')
            directory = self.directory(job)
            with self.lock(job):
                state = self.owned(job, request)
                if state['status'] != 'reserved' or (directory/'cancel.requested').exists():
                    raise HTTPException(status_code=409, detail='转换任务已取消或已提交，请重新上传')
                state.update(status='uploading', total=len(files), output_format=output_format)
                self.write(job, state)
            (directory/'inputs').mkdir()
            (directory/'outputs').mkdir()
            try:
                total, items = 0, []
                for index, file in enumerate(files, 1):
                    self.check_cancel(job)
                    name = file.filename.replace('\\', '/').rsplit('/', 1)[-1]
                    stem = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in Path(name).stem)[:64] or 'image'
                    file_path = directory/'inputs'/f'{index:04d}.image'
                    total += await self.save_upload(file, file_path, current_total=total, total_limit=self.max_batch_bytes,
                                                    cancel_marker=directory/'cancel.requested')
                    suffix = '.jpg' if output_format == 'jpeg' else '.png'
                    items.append({'index': index, 'name': name, 'file': file_path.name, 'output': f'{index:09d}_{stem}{suffix}'})
                (directory/'manifest.json').write_text(json.dumps({'options': {'format': output_format}, 'items': items}, ensure_ascii=False), encoding='utf-8')
                with self.lock(job):
                    self.check_cancel(job)
                    state['status'] = 'inspecting'
                    self.write(job, state)
            except BaseException as exc:
                cancelled = isinstance(exc, CompositionCancelled) or (directory/'cancel.requested').exists()
                self.finish(job, state, 'cancelled' if cancelled else 'failed', None if cancelled else '上传未完成，请重试')
                if isinstance(exc, CompositionCancelled):
                    raise HTTPException(status_code=409, detail='转换已取消') from None
                raise
            background_tasks.add_task(self.inspect, job)
            return {'id': job, 'status_url': f'/api/converter/{job}'}

        @router.post('/start')
        async def start(request: Request, background_tasks: BackgroundTasks):
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 65536:
                    raise HTTPException(status_code=413, detail='帧选择数据过大')
            try:
                selection = json.loads(data)
                job, frames = selection['id'], selection['frames']
                if not isinstance(job, str) or not isinstance(frames, dict):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise HTTPException(status_code=400, detail='帧选择格式不正确') from None
            directory = self.directory(job)
            with self.lock(job):
                state = self.owned(job, request)
                if state['status'] != 'selecting' or (directory/'cancel.requested').exists():
                    raise HTTPException(status_code=409, detail='转换任务已启动、已取消或已过期')
                manifest = json.loads((directory/'manifest.json').read_text('utf-8'))
                information = {item['index']: item for item in state['inputs']}
                for item in manifest['items']:
                    info = information[item['index']]
                    frame = frames.get(str(item['index']), 0 if info.get('frames', 0) == 1 else None)
                    if not info.get('error') and (type(frame) is not int or not 0 <= frame < info['frames']):
                        raise HTTPException(status_code=400, detail=f"请为「{item['name']}」选择有效的帧/页")
                    item['frame'] = frame
                (directory/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
                state.update(status='queued', done=0)
                self.write(job, state)
            background_tasks.add_task(self.run, job)
            return {'id': job}

        @router.get('/{job}')
        def status(job: str, request: Request):
            state = self.owned(job, request)
            hidden = {'owner'}
            if state['status'] not in FINISHED | {'selecting'}:
                hidden.update({'images', 'inputs', 'skipped'})
            return JSONResponse({key: value for key, value in state.items() if key not in hidden}, headers={'Cache-Control': 'private, no-store'})

        @router.post('/{job}/cancel')
        def cancel(job: str, request: Request):
            with self.lock(job):
                state = self.owned(job, request)
                if state['status'] not in FINISHED:
                    (self.directory(job)/'cancel.requested').touch()
                    idle = state['status'] in {'reserved', 'selecting'}
                    state['status'] = 'cancelled' if idle else 'cancelling'
                    if idle:
                        if not any(self.directory(job).glob('preview-*.lease')):
                            shutil.rmtree(self.directory(job)/'inputs', ignore_errors=True)
                        state['finished_at'] = datetime.now(timezone.utc).isoformat()
                    self.write(job, state)
                return {'status': state['status']}

        @router.get('/{job}/frame/{index}/{frame}')
        def frame_preview(job: str, index: int, frame: int, request: Request):
            with self.lock(job):
                state = self.owned(job, request)
                if state['status'] != 'selecting':
                    raise HTTPException(status_code=409, detail='此任务当前不能选择帧，请重新上传')
                item = next((item for item in state['inputs'] if item['index'] == index), None)
                if not item or item.get('error') or not 0 <= frame < item.get('frames', 0):
                    raise HTTPException(status_code=400, detail='所选帧/页不存在')
                manifest = json.loads((self.directory(job)/'manifest.json').read_text('utf-8'))
                source = self.directory(job)/'inputs'/manifest['items'][index-1]['file']
                lease = self.directory(job)/f'preview-{uuid.uuid4().hex}.lease'
                lease.touch()
            # Do not hold the task lock while waiting for shared CPU capacity:
            # users must still be able to cancel a queued or running preview.
            try:
                content = submit_composition(_preview, str(source), frame, str(self.directory(job)/'cancel.requested'),
                                             estimated_bytes=estimate_conversion_memory(source), cancel_check=lambda: self.check_cancel(job)).result()
            except Exception as exc:
                raise HTTPException(status_code=400, detail='预览失败：'+str(exc)) from None
            finally:
                with self.lock(job):
                    lease.unlink(missing_ok=True)
                    if self.read(job)['status'] in FINISHED and not any(self.directory(job).glob('preview-*.lease')):
                        shutil.rmtree(self.directory(job)/'inputs', ignore_errors=True)
            return Response(content, media_type='image/png', headers={'Cache-Control': 'private, max-age=3600'})

        def output_path(directory, item):
            path = directory/'outputs'/Path(item['name']).name
            if not path.is_file():
                raise HTTPException(status_code=404, detail='转换图片已过期')
            return path

        def result_file(job, image, request):
            state = self.owned(job, request)
            item = next((item for item in state['images'] if item['id'] == image), None)
            if not item:
                raise HTTPException(status_code=404, detail='转换图片不存在')
            return output_path(self.directory(job), item)

        @router.get('/{job}/images/{image}')
        def preview(job: str, image: str, request: Request):
            path = result_file(job, image, request)
            return FileResponse(path, media_type='image/png' if path.suffix == '.png' else 'image/jpeg',
                                headers={'Cache-Control': 'private, no-store'})

        @router.get('/{job}/images/{image}/thumb')
        def thumbnail(job: str, image: str, request: Request):
            source = result_file(job, image, request)
            directory = source.parent.parent
            target = directory/'thumbnails'/(source.stem+'.webp')
            headers = {'Cache-Control': 'private, no-cache'}
            lock = self.thumbnail_locks[hash((job, image)) % len(self.thumbnail_locks)]
            with lock:
                if not target.is_file():
                    content = submit_composition(result_thumbnail, str(source),
                                                 estimated_bytes=estimate_conversion_memory(source)).result()
                    temporary = target.with_suffix('.tmp')
                    try:
                        ensure_space(directory, len(content))
                        target.parent.mkdir(exist_ok=True)
                        temporary.write_bytes(content)
                        temporary.replace(target)
                    except OSError:
                        temporary.unlink(missing_ok=True)
                        return Response(content, media_type='image/webp', headers=headers)
            return FileResponse(target, media_type='image/webp', headers=headers)

        @router.get('/{job}/images/{image}/download')
        def download(job: str, image: str, request: Request):
            path = result_file(job, image, request)
            return FileResponse(path, filename=path.name, media_type='image/png' if path.suffix == '.png' else 'image/jpeg',
                                headers={'Cache-Control': 'private, no-store'})

        @router.post('/{job}/download')
        def pack(job: str, request: Request):
            with self.lock(job):
                state = self.owned(job, request)
                if state['status'] not in FINISHED or not state['images']:
                    raise HTTPException(status_code=409, detail='请先完成转换再下载')
                ticket = uuid.uuid4().hex
                directory = self.directory(job)
                path = directory/f'download-{ticket}.zip'
                try:
                    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
                        for image in sorted(state['images'], key=lambda image: image['id']):
                            source = output_path(directory, image)
                            with source.open('rb') as incoming, archive.open(source.name, 'w', force_zip64=True) as outgoing:
                                while chunk := incoming.read(1024**2):
                                    ensure_space(path.parent, len(chunk)+4096)
                                    outgoing.write(chunk)
                except BaseException as exc:
                    path.unlink(missing_ok=True)
                    if isinstance(exc, StorageFull):
                        raise HTTPException(status_code=507, detail=str(exc)) from None
                    raise
                return {'url': f'/api/converter/{job}/downloads/{ticket}'}

        @router.get('/{job}/downloads/{ticket}')
        def archive(job: str, ticket: str, request: Request, background_tasks: BackgroundTasks):
            self.owned(job, request)
            if not re.fullmatch(r'[a-f0-9]{32}', ticket):
                raise HTTPException(status_code=404, detail='下载文件不存在')
            path = self.directory(job)/f'download-{ticket}.zip'
            if not path.is_file():
                raise HTTPException(status_code=404, detail='下载已过期，请重新打包')
            background_tasks.add_task(path.unlink, missing_ok=True)
            return FileResponse(path, filename='converted-images.zip', media_type='application/zip',
                                headers={'Cache-Control': 'private, no-store'}, background=background_tasks)
        return router
