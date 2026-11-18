import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from food_compositor import oss_rest
from food_compositor.composition_archive import CompositionArchive, history_day
from food_compositor.record_cache import RecordCache, read_records


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.reads = []
        self.bucket = SimpleNamespace(get_object=self.get, put_object=self.put,
                                      put_object_from_file=lambda key, path: self.put(key, Path(path).read_bytes()),
                                      delete_object=lambda key: self.objects.pop(key, None))
        self.sdk = SimpleNamespace(exceptions=oss_rest.exceptions, ObjectIteratorV2=self.list)

    def get(self, key):
        self.reads.append(key)
        if key not in self.objects:
            raise oss_rest.NoSuchKey(key)
        return io.BytesIO(self.objects[key])

    def put(self, key, data):
        self.objects[key] = data.encode() if isinstance(data, str) else data

    def list(self, bucket, prefix):
        return [SimpleNamespace(key=key, etag=str(hash(data))) for key, data in self.objects.items() if key.startswith(prefix)]


class CatalogCacheTest(unittest.TestCase):
    def test_refresh_downloads_only_changed_metadata(self):
        storage = FakeStorage()
        prefix = 'records/'
        storage.put(prefix + '0000000000000001.json', json.dumps({'id': '0000000000000001', 'name': 'first'}))
        load = lambda old, versions: read_records(storage.bucket, storage.sdk, prefix, old, versions, server._TEMPLATE_ID_RE)
        cache = RecordCache()
        cache.get('key', load)
        cache.get('key', load, force=True)
        self.assertEqual(len(storage.reads), 1)
        storage.put(prefix + '0000000000000001.json', json.dumps({'id': '0000000000000001', 'name': 'changed'}))
        self.assertEqual(cache.get('key', load, force=True)[0][0]['name'], 'changed')
        self.assertEqual(len(storage.reads), 2)

    def test_slow_refresh_never_overwrites_concurrent_additions_and_deletions(self):
        started, finish = threading.Event(), threading.Event()
        cache = RecordCache(ttl=0)
        cache.get('key', lambda *_: ([{'id': 'old'}], {}))
        def slow(*_):
            started.set()
            finish.wait(2)
            return [{'id': 'old'}], {}
        old, stale = cache.get('key', slow)
        self.assertTrue(stale)
        self.assertTrue(started.wait(1))
        cache.update('key', 'old', None)
        cache.update('key', 'new', {'id': 'new'})
        with cache.lock:
            pending = cache.entries['key']['pending']
        finish.set()
        self.assertTrue(pending.wait(2))
        self.assertEqual(cache.peek('key', 'new'), {'id': 'new'})
        self.assertIsNone(cache.peek('key', 'old'))

    def test_disk_snapshot_survives_restart_and_failure_but_not_wrong_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            disk = Path(directory) / 'catalog.json'
            RecordCache().get('bucket-A', lambda *_: ([{'id': 'a'}], {}), disk=disk)
            def failure(*_):
                raise OSError('offline')
            restarted = RecordCache()
            records, stale = restarted.get('bucket-A', failure, disk=disk, force=True)
            self.assertTrue(stale)
            self.assertEqual(records, [{'id': 'a'}])
            with self.assertRaises(RuntimeError):
                RecordCache().get('bucket-B', failure, disk=disk)


class ArchiveDatesAndPagesTest(unittest.TestCase):
    def test_delete_and_restore_preserve_original_day_and_expiry(self):
        storage = FakeStorage()
        archive = CompositionArchive(storage.bucket, storage.sdk, 'archive/')
        owner, entry_id = 'a' * 64, 'b' * 12 + '-000000001'
        clock = datetime.now(timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock
        with tempfile.TemporaryDirectory() as directory, patch('food_compositor.composition_archive.datetime', Clock):
            path = Path(directory) / 'photo.jpg'
            path.write_bytes(b'photo')
            original = archive.save(owner, entry_id, {'product_name': 'food', 'template_name': 'poster'}, path, 'history')
            clock += timedelta(days=1)
            trashed = archive.trash(owner, entry_id)
            restored = archive.restore(owner, entry_id)
            self.assertEqual(original['downloaded_at'], restored['downloaded_at'])
            self.assertEqual(history_day(original), history_day(restored))
            self.assertEqual(original['saved_at'], restored['saved_at'])
            self.assertEqual(trashed['expires_at'], restored['expires_at'])
            self.assertNotEqual(original['updated_at'], restored['updated_at'])

    def test_folders_pages_and_concurrent_delete_do_not_skip_records(self):
        storage = FakeStorage()
        client = TestClient(server.app)
        token = '1' * 64
        client.cookies.set(server.WORKSPACE_COOKIE, token)
        import hashlib
        owner = hashlib.sha256(token.encode()).hexdigest()
        today = datetime.now(timezone.utc)
        day = today.astimezone(timezone(timedelta(hours=8))).date().isoformat()
        for index in range(1, 6):
            entry_id = f"{'f' * 12}-{index:09d}"
            record = {'id': entry_id, 'saved_at': today.isoformat(), 'section': 'history',
                      'expires_at': (today + timedelta(days=7)).isoformat(), 'name': f'{index}.jpg',
                      'product_name': 'product', 'template_name': 'template'}
            storage.put(f'{server.ARCHIVE_PREFIX}{owner}/{entry_id}.json', json.dumps(record))
        with patch.object(server, '_oss_bucket', return_value=storage.bucket), patch.object(server, '_oss_sdk', return_value=storage.sdk):
            folders = client.get('/api/composition-archive?view=folders').json()
            self.assertEqual(folders['folders'], [{'day': day, 'count': 5}])
            first = client.get('/api/composition-archive', params={'view': 'page', 'day': day, 'limit': 2}).json()
            self.assertEqual(len(first['items']), 2)
            self.assertEqual(client.delete('/api/composition-archive/' + first['items'][0]['id']).status_code, 200)
            second = client.get('/api/composition-archive', params={'view': 'page', 'day': day, 'limit': 2, 'cursor': first['next_cursor']}).json()
            self.assertEqual([entry['id'][-1] for entry in second['items']], ['3', '2'])
            self.assertEqual(client.get('/api/composition-archive', params={'view': 'page', 'section': 'trash', 'cursor': first['next_cursor']}).status_code, 400)
            self.assertEqual(client.get('/api/composition-archive', params={'view': 'page', 'cursor': '!'}).status_code, 400)
            self.assertEqual(client.get('/api/composition-archive?view=folders').json()['folders'][0]['count'], 4)
            self.assertEqual(TestClient(server.app).get('/api/composition-archive?view=folders').json()['folders'], [])
