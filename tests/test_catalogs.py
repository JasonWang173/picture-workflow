import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from food_compositor import composition_archive, oss_rest
from food_compositor.composition_archive import CompositionArchive, history_day
from food_compositor.record_cache import CatalogPending, RecordCache, read_records


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
    def test_restart_uses_durable_archive_for_shared_and_private_views(self):
        storage = FakeStorage()
        storage.bucket.bucket_name = 'example'
        storage.bucket.endpoint = 'https://oss.example'
        owner, other = 'a' * 64, 'b' * 64
        entry_id = '123456789abc-000000001'
        image = {'product_name': 'food', 'template_name': 'poster'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = CompositionArchive(storage.bucket, storage.sdk, 'archive/', cache_dir=root)
            photo = root / 'one.jpg'
            photo.write_bytes(b'photo')
            with patch('food_compositor.composition_archive._catalogs', RecordCache()):
                archive.list(None)
                archive.save(owner, entry_id, image, photo, 'history')
                archive.save(other, entry_id, image, photo, 'history')
                archive.trash(owner, entry_id)
            # A new process opens the same complete directory, including subsequent writes.
            with patch('food_compositor.composition_archive._catalogs', RecordCache()), \
                    patch.object(storage.sdk, 'ObjectIteratorV2', side_effect=AssertionError('unexpected OSS listing')) as listing:
                private = archive.list(owner)
                self.assertEqual([(item['id'], item['section']) for item in private], [(entry_id, 'trash')])
                self.assertNotIn('archive_key', private[0])
                self.assertEqual([item['section'] for item in archive.list(other)], ['history'])
                self.assertEqual({r['archive_key'] for r in archive.list(None)}, {f'{owner}~{entry_id}', f'{other}~{entry_id}'})
                archive.restore(owner, entry_id)
                listing.assert_not_called()
            # An old directory stays readable during a slow refresh; expiry is checked at read time.
            with closing(sqlite3.connect(archive.snapshot.path)) as db, db:
                db.execute("UPDATE catalog SET synced_at = synced_at - 3600")
                key = f'{other}/{entry_id}'
                payload = json.loads(db.execute("SELECT payload FROM records WHERE id=?", (key,)).fetchone()[0])
                payload['expires_at'] = '2020-01-01T00:00:00+00:00'
                db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload), key))
            started, finish = threading.Event(), threading.Event()
            def slow_list(*args, **kwargs):
                started.set()
                finish.wait(4)
                return storage.list(*args, **kwargs)
            cache = RecordCache(ttl=composition_archive._catalogs.ttl, max_stale=composition_archive._catalogs.max_stale)
            with patch('food_compositor.composition_archive._catalogs', cache), patch.object(storage.sdk, 'ObjectIteratorV2', slow_list):
                try:
                    visible = archive.list(None)
                    self.assertTrue(started.wait(1))
                    self.assertEqual([(r['archive_key'], r['section']) for r in visible], [(f'{owner}~{entry_id}', 'history')])
                    self.assertEqual(archive.list(other), [])
                finally:
                    finish.set()
                    archive.list(None, refresh=True)
            alternate = CompositionArchive(storage.bucket, storage.sdk, 'different/', cache_dir=root)
            self.assertNotEqual(alternate.snapshot.path, archive.snapshot.path)

    def test_cold_requests_join_background_sync_and_never_publish_an_empty_directory(self):
        cache = RecordCache()
        started, finish = threading.Event(), threading.Event()
        def slow(*args):
            started.set()
            finish.wait(3)
            return [{'id': 'one'}], {}
        try:
            with self.assertRaises(CatalogPending):
                cache.get('cold', slow, timeout=0)
            self.assertTrue(started.wait(1))
            with self.assertRaises(CatalogPending):
                cache.get('cold', lambda *_: self.fail('duplicate listing'), timeout=0)
        finally:
            finish.set()
        self.assertEqual(cache.get('cold', slow)[0], [{'id': 'one'}])

    def test_catalog_build_tells_client_to_retry_but_denied_team_access_does_not(self):
        storage = FakeStorage()
        client = TestClient(server.app)
        self.addCleanup(client.close)
        with patch.object(server, '_oss_bucket', return_value=storage.bucket), patch.object(server, '_oss_sdk', return_value=storage.sdk), \
                patch.object(CompositionArchive, 'list', side_effect=CatalogPending()) as listing:
            pending = client.get('/api/composition-archive?view=batch_folders&scope=browser')
            self.assertEqual(pending.status_code, 503)
            self.assertEqual(pending.headers['retry-after'], '5')
            denied = client.get('/api/composition-archive?view=batch_folders&scope=team')
            self.assertEqual(denied.status_code, 401)
            self.assertNotIn('retry-after', denied.headers)
            listing.assert_called_once()

    def test_history_orders_existing_batches_and_pages_by_beijing_day_and_entry_time(self):
        storage = FakeStorage()
        client = TestClient(server.app)
        self.addCleanup(client.close)
        token = '2' * 64
        client.cookies.set(server.WORKSPACE_COOKIE, token)
        owner = hashlib.sha256(token.encode()).hexdigest()
        endpoint = '/api/composition-archive'
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        folder = '2026-11-24-zs（10）'
        other = '2026-11-24-zs（2）'
        morning = '2026-11-24-aa（1）'
        yesterday = '2026-11-23-zs（1）'

        def put(index, group, saved, downloaded=None, section='history', expires_at=expires):
            entry_id = f'aaaaaaaaaaaa-{index:09d}'
            record = {'id': entry_id, 'section': section, 'saved_at': saved, 'expires_at': expires_at}
            if group:
                record['download_folder'] = group
            if downloaded:
                record['downloaded_at'] = downloaded
            storage.put(f'{server.ARCHIVE_PREFIX}{owner}/{entry_id}.json', json.dumps(record))

        preceding = datetime.fromisoformat(expires) - timedelta(hours=1)
        put(1, folder, '2026-11-24T09:30:00+08:00', expires_at=preceding.isoformat())
        put(2, folder, '2026-11-24T00:00:01+00:00', '2026-11-24T11:00:00+08:00')
        put(3, folder, '2026-11-24T03:00:00+00:00')  # Same instant as image 2.
        put(4, folder, '2026-11-24T02:00:00+00:00')
        put(5, other, '2026-11-24T11:00:00+08:00')
        put(6, morning, '2026-11-24T01:00:00+00:00')
        put(7, None, '2026-11-23T16:00:00+00:00')  # Midnight in a legacy Beijing day folder.
        put(8, yesterday, '2026-11-24T05:00:00+00:00')  # Later addition to an older batch.
        put(9, morning, '2026-11-24T15:00:00+08:00', section='trash',
            expires_at=(preceding - timedelta(days=1)).isoformat())
        with patch.object(server, '_oss_bucket', return_value=storage.bucket), patch.object(server, '_oss_sdk', return_value=storage.sdk):
            directory = client.get(endpoint, params={'view': 'batch_folders'}).json()
            groups = directory['folders']
            first_ms = int(preceding.timestamp() * 1000)
            self.assertEqual(groups[0]['expiry_offsets_ms'], [first_ms, 3600000, 0, 0])
            self.assertEqual(datetime.fromisoformat(directory['expires_at']).timestamp(), first_ms / 1000)
            self.assertEqual([g['folder'] for g in groups], [folder, other, morning, '2026-11-24', yesterday])
            self.assertEqual([g.get('username') for g in groups], ['zs', 'zs', 'aa', None, 'zs'])
            self.assertEqual(groups[0]['latest_at'], '2026-11-24T03:00:00.000000+00:00')
            self.assertEqual(groups[0]['count'], 4)
            params = {'view': 'page', 'folder': folder, 'order': 'recent', 'limit': 1}
            first = client.get(endpoint, params=params).json()
            self.assertEqual(first['total'], 4)
            self.assertEqual(first['items'][0]['id'][-1], '3')
            cursor = first['next_cursor']
            self.assertEqual(client.delete(endpoint + '/' + first['items'][0]['id']).status_code, 200)
            self.assertEqual(client.get(endpoint, params={'view': 'page', 'folder': folder, 'cursor': cursor}).status_code, 400)
            remaining = client.get(endpoint, params={**params, 'cursor': cursor, 'limit': 3}).json()
            self.assertEqual([entry['id'][-1] for entry in remaining['items']], ['2', '4', '1'])
            self.assertIsNone(remaining['next_cursor'])

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
