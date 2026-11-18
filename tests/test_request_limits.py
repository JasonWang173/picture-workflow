import asyncio
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient

from food_compositor.request_limits import RequestLimits


class RequestLimitTest(unittest.TestCase):
    def test_oversized_chunked_body_is_rejected_before_composition(self):
        app = FastAPI()
        calls = []
        @app.post('/api/compose')
        async def compose(file: UploadFile = File()):
            calls.append(True)
            return {'ok': True}
        with tempfile.TemporaryDirectory() as directory:
            gate = RequestLimits(app, directory)
            gate.max_bytes = 32
            async def check():
                sent = []
                messages = iter([{'type': 'http.request', 'body': b'x' * 64, 'more_body': False}])
                async def receive():
                    return next(messages)
                async def send(message):
                    sent.append(message)
                scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'method': 'POST', 'path': '/api/compose',
                         'headers': [(b'content-type', b'multipart/form-data; boundary=test')], 'query_string': b'',
                         'client': ('test', 1), 'server': ('test', 80), 'scheme': 'http', 'root_path': ''}
                await gate(scope, receive, send)
                return sent
            messages = asyncio.run(check())
            self.assertEqual(messages[0]['status'], 413)
            self.assertEqual(calls, [])
            self.assertEqual(gate.active, {})

    def test_rate_limit_does_not_block_read_routes(self):
        app = FastAPI()
        @app.post('/api/compose')
        async def compose():
            return {'ok': True}
        @app.get('/api/health')
        async def health():
            return {'status': 'ok'}
        with tempfile.TemporaryDirectory() as directory:
            app.add_middleware(RequestLimits, storage=directory)
            with patch.dict('os.environ', {'MAX_TASKS_PER_MINUTE': '1'}):
                client = TestClient(app)
                self.assertEqual(client.post('/api/compose').status_code, 200)
                response = client.post('/api/compose')
                self.assertEqual(response.status_code, 429)
                self.assertIn('Retry-After', response.headers)
                self.assertEqual(client.get('/api/health').status_code, 200)
