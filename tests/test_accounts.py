import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from food_compositor.account_api import create_account_router
from food_compositor.accounts import AccountStore

SITE = 'https://workbench.test'
COOKIE = '__Host-workflow_session'
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

    def test_registration_logs_in_and_rejects_duplicate_or_invalid_accounts(self):
        register = '/api/auth/register'
        payload = {'username': 'Zs', 'password': PASSWORD}
        self.assertEqual(self.client.post(register, json={**payload, 'password': 'short'}).status_code, 400)
        self.assertEqual(self.client.post(register, json=payload, headers={'Origin': 'https://other.test'}).status_code, 403)
        self.assertEqual(self.client.post(register, json=payload).status_code, 201)
        expected = {'username': 'zs', 'display_name': 'zs'}
        self.assertEqual(self.client.get('/api/auth/session').json()['account'], expected)
        self.assertEqual(self.client.post(register, json={**payload, 'username': 'ZS'}).status_code, 409)
        self.client.post('/api/auth/logout', json={})
        self.assertEqual(self.login(username='ZS').json()['account'], expected)

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
