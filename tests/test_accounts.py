import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from food_compositor.account_api import create_account_router
from food_compositor.accounts import AccountStore, REMEMBER_SECONDS

SITE = 'https://workbench.test'
COOKIE = '__Host-workflow_session'
DEVICE = COOKIE + '_device'
PASSWORD = 'only a test password 25'


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'accounts.sqlite3'
        self.store = AccountStore(self.path)
        self.store.create('Alice', '小李', PASSWORD)
        self.client = self.make_client()

    def make_client(self, base=SITE, **kwargs):
        app = FastAPI()
        app.include_router(create_account_router(AccountStore(self.path)))
        client = TestClient(app, base_url=base, **kwargs)
        self.addCleanup(client.close)
        return client

    def login(self, client=None, password=PASSWORD, username='Alice'):
        return (client or self.client).post('/api/auth/login', json={'username': username, 'password': password})

    def test_login_survives_reload_and_new_app_instance_then_logout_revokes_token(self):
        self.assertIsNone(self.client.get('/api/auth/session').json()['account'])
        wrong = self.login(password='incorrect')
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json(), self.login(username='nobody').json())
        logged_in = self.login()
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        expected = {'username': 'alice', 'display_name': '小李'}
        self.assertEqual(logged_in.json()['account'], expected)
        for flag in ('HttpOnly', 'Secure', 'SameSite=strict', 'Path=/'):
            self.assertIn(flag, logged_in.headers['set-cookie'])
        self.assertEqual(logged_in.headers['cache-control'], 'no-store')
        token = self.client.cookies.get(COOKIE)
        self.assertNotIn(PASSWORD.encode(), self.path.read_bytes())
        self.assertNotIn(token.encode(), self.path.read_bytes())

        recovered = self.make_client()
        recovered.cookies.set(COOKIE, token)
        self.assertEqual(recovered.get('/api/auth/session').json()['account'], expected)
        self.assertEqual(self.client.post('/api/auth/logout', json={}).status_code, 200)
        self.assertIsNone(recovered.get('/api/auth/session').json()['account'])

    def test_password_reset_disablement_and_expiry_invalidate_sessions(self):
        self.assertEqual(self.login().status_code, 200)
        self.store.reset_password('alice', 'another test password 26')
        self.assertIsNone(self.client.get('/api/auth/session').json()['account'])
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password='another test password 26').status_code, 200)
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute('UPDATE account_sessions SET expires_at = 0')
        self.assertIsNone(self.client.get('/api/auth/session').json()['account'])
        self.assertEqual(self.login(password='another test password 26').status_code, 200)
        self.store.set_enabled('alice', False)
        self.assertIsNone(self.client.get('/api/auth/session').json()['account'])
        self.assertEqual(self.login(password='another test password 26').status_code, 401)
        self.store.set_enabled('alice', True)
        self.assertEqual(self.login(password='another test password 26').status_code, 200)

    def test_login_transport_origin_and_failed_attempt_limits(self):
        http = self.make_client('http://workbench.test')
        self.assertEqual(self.login(http).status_code, 403)
        local = self.make_client('http://127.0.0.1', client=('127.0.0.1', 12345))
        self.assertEqual(self.login(local).status_code, 200)
        self.assertEqual(local.get('/api/auth/session').json()['account']['username'], 'alice')
        wrong_origin = self.client.post('/api/auth/login', headers={'origin': 'https://elsewhere.test'},
                                        json={'username': 'Alice', 'password': PASSWORD})
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(self.client.post('/api/auth/login', files={'username': (None, 'alice')}).status_code, 415)
        for _ in range(12):
            self.assertEqual(self.login(username='!').status_code, 401)
        self.assertEqual(self.login(username='!').status_code, 429)

    def test_signing_in_with_an_existing_cookie_rotates_the_token(self):
        self.assertEqual(self.login().status_code, 200)
        original = self.client.cookies.get(COOKIE)
        self.assertEqual(self.login().status_code, 200)
        self.assertNotEqual(self.client.cookies.get(COOKIE), original)
        self.assertIsNone(self.store.session(original))

    def test_change_password_signs_out_every_device_then_new_login_can_remember_again(self):
        other = self.make_client()
        temporary = self.make_client()
        coworker = self.make_client()
        self.store.create('zs', '', PASSWORD)
        self.login(coworker, username='zs').raise_for_status()
        self.login().raise_for_status()
        self.login(other).raise_for_status()
        temporary.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD, 'remember_device': False}).raise_for_status()
        old_sessions = [c.cookies.get(COOKIE) for c in (self.client, other, temporary)]
        old_devices = [c.cookies.get(DEVICE) for c in (self.client, other)]
        payload = {'username': 'alice', 'current_password': PASSWORD, 'new_password': '新'}
        changed = self.client.post('/api/auth/change-password', json=payload)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json(), {'account': None, 'remembered': None})
        self.assertIsNone(self.client.cookies.get(COOKIE))
        self.assertIsNone(self.client.cookies.get(DEVICE))
        for token in old_sessions:
            self.assertIsNone(self.store.session(token))
        for device in old_devices:
            self.assertIsNone(self.store.remembered_account(device))
        self.assertEqual(other.post('/api/auth/resume/alice', json={}).status_code, 401)
        self.assertEqual(self.login(other).status_code, 401)
        self.assertEqual(coworker.get('/api/auth/session').json()['account']['username'], 'zs')
        for client in (self.client, other):
            self.login(client, password='新').raise_for_status()
            self.assertNotIn(client.cookies.get(DEVICE), old_devices)
            client.post('/api/auth/logout?forget=false', json={}).raise_for_status()
            self.assertEqual(client.post('/api/auth/resume/alice', json={}).json()['account']['username'], 'alice')

    def test_change_password_requires_active_session_and_original_password(self):
        endpoint = '/api/auth/change-password'
        valid = {'username': 'alice', 'current_password': PASSWORD, 'new_password': 'new'}
        self.assertEqual(self.client.post(endpoint, json=valid).status_code, 401)
        self.login().raise_for_status()
        self.assertEqual(self.client.post(endpoint, json=valid, headers={'Origin': 'https://other.test'}).status_code, 403)
        self.assertEqual(self.client.post(endpoint, json={**valid, 'username': 'zs'}).status_code, 409)
        for invalid in ({'current_password': 'incorrect'}, {'new_password': ''}, {'new_password': PASSWORD}, {'new_password': 42}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.client.post(endpoint, json={**valid, **invalid}).status_code, 400)
        self.assertEqual(self.client.get('/api/auth/session').json()['account']['username'], 'alice')
        self.client.post('/api/auth/logout?forget=false', json={}).raise_for_status()
        self.assertEqual(self.client.post(endpoint, json=valid).status_code, 401)
        self.login().raise_for_status()

    def test_revocation_during_password_verification_cannot_commit_change(self):
        self.login().raise_for_status()
        token = self.client.cookies.get(COOKIE)
        from food_compositor.accounts import _new_password

        def revoke_before_commit(password):
            self.store.sign_out(token)
            return _new_password(password)

        with patch('food_compositor.accounts._new_password', side_effect=revoke_before_commit):
            self.assertFalse(self.store.change_password(token, 'alice', PASSWORD, 'new'))
        self.login().raise_for_status()

    def test_browser_restart_offers_account_and_requires_click_until_fixed_deadline(self):
        now = int(time.time())
        with patch('food_compositor.accounts.time.time', return_value=now):
            login = self.login()
        device_header = next(h for h in login.headers.get_list('set-cookie') if h.startswith(DEVICE + '='))
        self.assertIn('Max-Age=2592000', device_header)
        for flag in ('HttpOnly', 'Secure', 'SameSite=strict', 'Path=/'):
            self.assertIn(flag, device_header)
        session_header = next(h for h in login.headers.get_list('set-cookie') if h.startswith(COOKIE + '='))
        self.assertNotIn('Max-Age', session_header)
        device = self.client.cookies.get(DEVICE)
        self.assertNotIn(device.encode(), self.path.read_bytes())
        self.assertIsNone(self.store.session(device))
        self.assertIsNone(self.store.remembered_account(self.client.cookies.get(COOKIE)))

        browser = self.make_client()
        browser.cookies.set(DEVICE, device, domain='workbench.test')
        self.assertEqual(browser.get('/api/auth/session').json(), {'account': None, 'remembered': login.json()['account']})
        self.assertEqual(browser.post('/api/auth/resume/alice', json={}, headers={'origin': 'https://other.test'}).status_code, 403)
        self.assertEqual(browser.post('/api/auth/resume/someone', json={}).status_code, 401)
        with patch('food_compositor.accounts.time.time', return_value=now + REMEMBER_SECONDS - 1):
            resumed = browser.post('/api/auth/resume/alice', json={})
            self.assertEqual(resumed.status_code, 200)
            self.assertEqual(browser.get('/api/auth/session').json()['account'], login.json()['account'])
            self.assertFalse(any(h.startswith(DEVICE + '=') for h in resumed.headers.get_list('set-cookie')))
        with patch('food_compositor.accounts.time.time', return_value=now + REMEMBER_SECONDS):
            self.assertEqual(browser.get('/api/auth/session').json(), {'account': None, 'remembered': None})
            self.assertEqual(browser.post('/api/auth/resume/alice', json={}).status_code, 401)

    def test_logout_keep_forget_and_switch_affect_only_this_browser(self):
        other = self.make_client()
        self.login(other)
        self.login()
        device = self.client.cookies.get(DEVICE)
        active = self.client.cookies.get(COOKIE)
        paused = self.client.post('/api/auth/logout?forget=false', json={})
        self.assertIsNone(paused.json()['account'])
        self.assertEqual(paused.json()['remembered']['username'], 'alice')
        self.assertIsNone(self.store.session(active))
        self.assertEqual(self.client.post('/api/auth/resume/alice', json={}).status_code, 200)
        self.assertEqual(self.login(username='unknown').status_code, 401)
        self.assertEqual(self.client.get('/api/auth/session').json()['remembered']['username'], 'alice')
        replacement = self.client.post('/api/auth/register', json={'username': 'zs', 'password': 'x'})
        self.assertEqual(replacement.json()['remembered']['username'], 'zs')
        self.assertIsNone(self.store.remembered_account(device))
        replaced = self.client.cookies.get(DEVICE)
        self.client.post('/api/auth/logout', json={})
        self.assertIsNone(self.store.remembered_account(replaced))
        self.assertIsNone(self.client.cookies.get(DEVICE))
        self.assertEqual(self.client.post('/api/auth/resume/zs', json={}).status_code, 401)
        self.assertEqual(other.post('/api/auth/resume/alice', json={}).status_code, 200)

    def test_opt_out_password_reset_and_disablement_revoke_remembered_login(self):
        self.login()
        old = self.client.cookies.get(DEVICE)
        result = self.client.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD, 'remember_device': False})
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json()['remembered'])
        self.assertIsNone(self.store.remembered_account(old))
        self.assertIsNone(self.client.cookies.get(DEVICE))
        self.login()
        self.store.reset_password('alice', PASSWORD + '2')
        self.assertEqual(self.client.post('/api/auth/resume/alice', json={}).status_code, 401)
        self.login(password=PASSWORD + '2')
        self.store.set_enabled('alice', False)
        self.store.set_enabled('alice', True)
        self.assertEqual(self.client.post('/api/auth/resume/alice', json={}).status_code, 401)

    def test_existing_session_table_upgrades_without_losing_active_sessions(self):
        login = self.login()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ALTER TABLE account_sessions RENAME TO original_sessions')
            db.execute('CREATE TABLE account_sessions(token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL, expires_at INTEGER NOT NULL)')
            db.execute('INSERT INTO account_sessions SELECT token_hash, account_id, expires_at FROM original_sessions')
            db.execute('DROP TABLE original_sessions')
            db.execute('DROP TABLE remembered_devices')
        self.assertEqual(self.client.get('/api/auth/session').json(), {'account': login.json()['account'], 'remembered': None})
        self.login()
        self.assertEqual(self.client.post('/api/auth/resume/alice', json={}).status_code, 200)

    def test_registration_logs_in_and_rejects_duplicate_or_invalid_accounts(self):
        register = '/api/auth/register'
        payload = {'username': 'Zs', 'password': PASSWORD}
        self.assertEqual(self.client.post(register, json={**payload, 'password': ''}).status_code, 400)
        self.assertEqual(self.client.post(register, json=payload, headers={'Origin': 'https://other.test'}).status_code, 403)
        self.assertEqual(self.client.post(register, json=payload).status_code, 201)
        expected = {'username': 'zs', 'display_name': 'zs'}
        self.assertEqual(self.client.get('/api/auth/session').json()['account'], expected)
        self.assertEqual(self.client.post(register, json={**payload, 'username': 'ZS'}).status_code, 409)
        self.client.post('/api/auth/logout', json={})
        self.assertEqual(self.login(username='ZS').json()['account'], expected)

    def test_short_and_long_passwords_round_trip_without_truncation(self):
        for username, password in (('single', 'x'), ('longpass', '长🔑' * 6000 + '尾')):
            with self.subTest(username=username):
                response = self.client.post('/api/auth/register', json={'username': username, 'password': password})
                self.assertEqual(response.status_code, 201, response.text)
                self.client.post('/api/auth/logout', json={})
                self.assertEqual(self.login(username=username, password=password + '!').status_code, 401)
                self.assertEqual(self.login(username=username, password=password).status_code, 200)
        self.store.reset_password('alice', '新')
        self.assertEqual(self.login(password='新').status_code, 200)

    def test_new_accounts_accept_only_english_initials_including_one_letter(self):
        for name in ('zs1', 'z.s', 'z s', '张三', 'Ａ', 'K', '', 'a' * 33):
            with self.subTest(name=name):
                response = self.client.post('/api/auth/register', json={'username': name, 'password': PASSWORD})
                self.assertEqual(response.status_code, 400)
        for name in ('Q', 'a' * 32):
            response = self.client.post('/api/auth/register', json={'username': name, 'password': PASSWORD})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(self.login(username=name).json()['account']['username'], name.lower())

    def test_legacy_username_still_supports_login_and_maintenance(self):
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute("UPDATE accounts SET username = 'old-user.01' WHERE username = 'alice'")
        name = 'OLD-USER.01'
        self.assertEqual(self.login(username=name).status_code, 200)
        self.store.set_enabled(name, False)
        self.assertEqual(self.login(username=name).status_code, 401)
        self.store.set_enabled(name, True)
        password = 'a replacement password'
        self.store.reset_password(name, password)
        self.assertEqual(self.login(username=name, password=password).status_code, 200)
