import io
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import server
from food_compositor import core, execution
from food_compositor.cancellation import CompositionCancelled, cancellation_scope, check_cancelled


class BatchCancellationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.patch = patch.object(server, 'BATCH_ROOT', self.root)
        self.patch.start()
        server.app.middleware_stack = None
        self.client = TestClient(server.app)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.runner = ThreadPoolExecutor(max_workers=1)
        self.started = threading.Event()
        self.calls = []
        def submit(fn, *args, estimated_bytes=None, cancel_check=None, **kwargs):
            if cancel_check:
                cancel_check()
            return self.pool.submit(fn, *args, **kwargs)
        self.patches = [patch.object(server, 'submit_composition', side_effect=submit),
                        patch.object(server, 'worker_count', return_value=1),
                        patch.object(server, 'compose', side_effect=self.compose)]
        for mock in self.patches:
            mock.start()

    def tearDown(self):
        self.runner.shutdown(wait=True)
        self.pool.shutdown(wait=True)
        for mock in reversed(self.patches):
            mock.stop()
        self.client.close()
        self.patch.stop()
        self.directory.cleanup()

    def compose(self, product, template, output, **kwargs):
        self.calls.append((Path(template).name, Path(product).name if product else None))
        if product and Path(product).name == 'second.png':
            self.started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                check_cancelled()
                time.sleep(.01)
        Image.new('RGB', (80,100), 'blue').save(output)

    def prepare(self):
        task = self.client.post('/api/batches/reserve').json()
        directory = self.root/task['batch_id']
        work = directory/'work'
        work.mkdir()
        (directory/'outputs').mkdir()
        for name in ('first.png', 'second.png', 'third.png', 'template.png', 'next-template.png'):
            Image.new('RGB',(80,100),'red').save(work/name)
        manifest = {'templates': [{'index':1,'work':'template.png','name':'one','mode':'cutout'},
                                  {'index':2,'work':'next-template.png','name':'two','mode':'cutout'}],
                    'items':[{'index':i,'work':name,'stem':name,'name':name} for i,name in enumerate(('first.png','second.png','third.png'),1)]}
        (work/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        status=server._read_batch_status(directory)
        status.update(status='queued', total=6)
        server._write_batch_status(directory,status)
        return task,directory

    def run_task(self,task):
        return self.runner.submit(server._run_batch_composition,task['batch_id'],'cutout','80x100',.78,True,100)

    def test_cancel_before_upload_is_idempotent_and_prevents_late_submission(self):
        task=self.client.post('/api/batches/reserve').json()
        url=f"/api/batches/{task['batch_id']}/cancel"
        self.assertEqual(TestClient(server.app).post(url).status_code,404)
        self.assertEqual(self.client.post(url).json()['status'],'cancelled')
        self.assertEqual(self.client.post(url).json()['status'],'cancelled')
        content=io.BytesIO()
        Image.new('RGB',(80,100),'red').save(content,'PNG')
        response=self.client.post('/api/compose-batch',data={'batch_id':task['batch_id'],'mode':'template_replace'},
                                  files={'templates':('template.png',content.getvalue(),'image/png')})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.client.get(task['status_url']).json()['status'],'cancelled')
        self.assertFalse(self.calls)

    def test_cancel_running_stops_later_products_and_templates_and_keeps_completed_image(self):
        task,directory=self.prepare()
        future=self.run_task(task)
        self.assertTrue(self.started.wait(2))
        self.assertEqual(self.client.post(f"/api/batches/{task['batch_id']}/cancel").json()['status'],'cancelling')
        future.result(timeout=3)
        status=self.client.get(task['status_url']).json()
        self.assertEqual(status['status'],'cancelled')
        self.assertEqual(status['done'],1)
        self.assertEqual(len(status['images']),1)
        self.assertEqual(status['skipped'],[])
        self.assertEqual(self.calls,[('template.png','first.png'),('template.png','second.png')])
        self.assertEqual(self.client.get(status['images'][0]['url']).status_code,200)
        self.assertFalse((directory/'work').exists())
        # A fresh task still uses the same pool successfully.
        self.started.clear()
        self.calls.clear()
        with patch.object(server,'compose',side_effect=lambda product,template,output,**kw: Image.new('RGB',(80,100),'red').save(output)):
            new_task,_=self.prepare()
            self.run_task(new_task).result(timeout=3)
        self.assertEqual(self.client.get(new_task['status_url']).json()['status'],'done')
        self.assertEqual(self.client.post(f"/api/batches/{new_task['batch_id']}/cancel").json()['status'],'done')

    def test_cancel_queued_does_not_start_any_image(self):
        task,directory=self.prepare()
        self.client.post(f"/api/batches/{task['batch_id']}/cancel")
        self.run_task(task).result(timeout=2)
        self.assertEqual(self.client.get(task['status_url']).json()['status'],'cancelled')
        self.assertEqual(self.calls,[])
        self.assertFalse((directory/'work').exists())


class CancellationCheckpointTest(unittest.TestCase):
    def test_jpeg_encoding_observes_marker_without_poisoning_next_call(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'cancel'
            image=Image.new('RGB',(80,100),'red')
            with cancellation_scope(marker):
                marker.touch()
                with self.assertRaises(CompositionCancelled):
                    core._encode_jpeg(image,100)
                marker.unlink()
            self.assertTrue(core._encode_jpeg(image,100))

    def test_waiting_for_memory_can_be_cancelled_without_reserving_capacity(self):
        release=threading.Event()
        cancelled=threading.Event()
        with patch.object(execution,'ProcessPoolExecutor',side_effect=lambda **kw: ThreadPoolExecutor(max_workers=1)), \
             patch.object(execution,'worker_count',return_value=1), patch.object(execution,'memory_budget',return_value=1024):
            try:
                first=execution.submit_composition(lambda:release.wait(3),estimated_bytes=512)
                def check():
                    if cancelled.is_set():
                        raise CompositionCancelled()
                with ThreadPoolExecutor(max_workers=1) as caller:
                    waiting=caller.submit(execution.submit_composition,str,'never',estimated_bytes=512,cancel_check=check)
                    cancelled.set()
                    with self.assertRaises(CompositionCancelled):
                        waiting.result(timeout=1)
                    self.assertEqual(execution._active,1)
                    release.set()
                    first.result(timeout=1)
            finally:
                release.set()
                execution.shutdown_executor()
