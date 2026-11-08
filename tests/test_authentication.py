"""Concurrent login admission stays bounded before password hashing starts."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import unittest
from unittest.mock import patch
from test_server import Fixture
import api
import authentication as auth
import database
import settings


class LoginTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        auth.LOGIN_FAILS.clear()

    def login(self, ip='198.51.100.10'):
        conn = database.db()
        try:
            request = api.Request('POST', '/', [], b'', ip)
            auth.api_login(request, conn, dict(username='wrong', password='wrong'))
            return request.response[0]
        finally:
            conn.close()

    def test_concurrent_failures_reserve_limit_before_hashing(self):
        entered, release, guard = threading.Event(), threading.Event(), threading.Lock()
        count = 0

        def verify(*_):
            nonlocal count
            with guard:
                count += 1
                if count == settings.LOGIN_MAX_FAILS:
                    entered.set()
            release.wait(10)
            return False

        with patch.object(auth.security, 'verify_password', verify), ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.login) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(3))
                completed = as_completed(futures, timeout=3)
                for _ in range(3):
                    self.assertEqual(next(completed).result(), 429)
            finally:
                release.set()
            self.assertEqual(sorted(f.result() for f in futures), [401] * 5 + [429] * 3)
        self.assertEqual(auth.LOGIN_PENDING, 0)
        self.assertEqual(self.login(), 429)

    def test_expired_sources_are_removed_and_exception_releases_slot(self):
        with patch.object(auth.time, 'time', return_value=1000), patch.object(auth.security, 'verify_password', return_value=False):
            self.assertEqual(self.login(), 401)
        with patch.object(auth.time, 'time', return_value=1000 + settings.LOGIN_LOCK + 1), patch.object(auth.security, 'verify_password', side_effect=RuntimeError('hash unavailable')):
            with self.assertRaises(RuntimeError):
                self.login('198.51.100.11')
        self.assertNotIn('198.51.100.10', auth.LOGIN_FAILS)
        self.assertEqual(auth.LOGIN_PENDING, 0)
        self.assertEqual(auth.LOGIN_FAILS['198.51.100.11']['pending'], 0)

    def test_global_concurrency_limit_applies_across_sources(self):
        with patch.object(auth, 'LOGIN_PENDING', auth.LOGIN_CONCURRENCY):
            with patch.object(auth.security, 'verify_password') as verify:
                self.assertEqual(self.login(), 429)
                verify.assert_not_called()
