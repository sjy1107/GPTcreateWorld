import functools
import http.cookiejar
import http.server
import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from yt_dlp.cookies import YoutubeDLCookieJar
from video_demo.browser_cookies import BrowserCookieError, BrowserSession, browser_root, discover_profiles, load_cookie_file, normalize_profile
from video_demo.core import Network, page_candidates
from video_demo.gui import Worker
from tests.integration_demo import FIXTURES, inspect, make_fixtures


class CookieImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_session_zero_httponly_bom_expiry_and_scope(self):
        path = self.root / 'cookies.txt'
        path.write_text('# Netscape HTTP Cookie File\n'
                        '#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic_session\n'
                        '.bilibili.com\tTRUE\t/\tTRUE\t1\told\tsynthetic_old\n'
                        '.example.com\tTRUE\t/\tTRUE\t0\tother\tsynthetic_other\n', encoding='utf-8-sig')
        jar = load_cookie_file(path, 'https://www.bilibili.com/video/test')
        self.assertEqual([c.name for c in jar], ['SESSDATA'])
        self.assertIsNone(next(iter(jar)).expires)
        self.assertIn('SESSDATA=synthetic_session', jar.get_cookie_header('https://www.bilibili.com/'))
        self.assertFalse(jar.get_cookie_header('https://example.com/'))

    def test_wrong_formats_and_malformed_lines_do_not_expose_values(self):
        cases = [(b'SQLite format 3\x00more bytes', '数据库'),
                 (b'{"sessionid":"SENSITIVE_SYNTHETIC"}', 'JSON'),
                 (b'# Netscape HTTP Cookie File\nwrong\tSENSITIVE_SYNTHETIC', '不完整')]
        for content, message in cases:
            path = self.root / 'bad.txt'
            path.write_bytes(content)
            stream = io.StringIO()
            with redirect_stderr(stream), redirect_stdout(stream), self.assertRaises(ValueError) as caught:
                load_cookie_file(path)
            self.assertIn(message, str(caught.exception))
            self.assertNotIn('SENSITIVE_SYNTHETIC', str(caught.exception) + stream.getvalue())

    def test_wrong_site_file_is_rejected(self):
        path = self.root / 'wrong.txt'
        path.write_text('# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tsession\tsynthetic\n')
        with self.assertRaisesRegex(ValueError, '当前网站'):
            load_cookie_file(path, 'https://www.bilibili.com/')

    def test_detected_profiles_and_cookie_database_paths(self):
        for name in ('Default', 'Profile 1'):
            folder = self.root / name / 'Network'
            folder.mkdir(parents=True)
            (folder / 'Cookies').write_bytes(b'fake database, not read')
        with patch('video_demo.browser_cookies.browser_root', return_value=self.root):
            self.assertEqual([p.name for p in discover_profiles('edge')], ['Default', 'Profile 1'])
            with self.assertRaises(BrowserCookieError):
                normalize_profile('edge', '')
        with self.assertRaises(BrowserCookieError):
            normalize_profile('edge', str(self.root))
        self.assertEqual(normalize_profile('edge', str(self.root / 'Default/Network/Cookies')), str(self.root / 'Default'))
        self.assertEqual(normalize_profile('edge', str(self.root / 'Default/Network')), str(self.root / 'Default'))
        with patch.dict(os.environ, {'TEST_COOKIE_PROFILE': str(self.root / 'Default')}):
            self.assertEqual(normalize_profile('edge', '"%TEST_COOKIE_PROFILE%"'), str(self.root / 'Default'))

    def test_cookie_count_does_not_mean_account_is_logged_in(self):
        cookie = http.cookiejar.Cookie(0, 'tracking', 'synthetic', None, False, '.bilibili.com', True, True, '/', True, True, None, True, None, None, {})
        session = BrowserSession('edge', ('bilibili.com',), (cookie,))
        self.assertFalse(session.login_marker)
        cookie.name = 'SESSDATA'
        self.assertTrue(session.login_marker)

    def test_authenticated_scan_and_worker_download_preserve_session_cookies(self):
        make_fixtures()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if 'test_session=synthetic_only' not in self.headers.get('Cookie', ''):
                    self.server.rejections += 1
                    self.send_error(401)
                    return
                self.server.authorized += 1
                if self.path.startswith('/private.html'):
                    body = b'<html><title>Authenticated test</title><video src="/first.mp4"></video></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return super().do_GET()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(FIXTURES)))
        server.rejections = server.authorized = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        path = self.root / 'session.txt'
        path.write_text('# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\ttest_session\tsynthetic_only\n')
        original = path.read_bytes()
        url = f'http://127.0.0.1:{server.server_port}/private.html'
        try:
            with patch.dict(os.environ, {'NO_PROXY': 'localhost,127.0.0.1,::1'}):
                self.assertEqual(len(page_candidates(url, threading.Event(), Network(cookie_file=str(path)))), 1)
                worker = Worker('analyze', url, Network(cookie_file=str(path)))
                worker.run()
                self.assertFalse(worker.error, worker.error)
                self.assertEqual(len(worker.result), 1)
                downloader = Worker('download', ([(0, worker.result[0], None)], self.root), Network(cookie_file=str(path)))
                downloader.run()
                self.assertFalse(downloader.error, downloader.error)
                self.assertEqual(downloader.result['ok'], 1)
            outputs = list(self.root.glob('*.mp4'))
            self.assertEqual(len(outputs), 1)
            inspect(outputs[0])
            self.assertGreater(server.authorized, 1)
            self.assertEqual(server.rejections, 0)
            self.assertEqual(path.read_bytes(), original)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
