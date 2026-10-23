"""Import compatibility and explicit login workflow, using synthetic data only."""
import functools
import http.server
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from tests.integration_demo import FIXTURES, inspect, make_fixtures
from tests.test_workflow_additions import WindowTestCase
from video_demo.browser_cookies import inspect_cookie_text, load_cookie_text
from video_demo.gui import Worker
from video_demo.help_ui import HelpDialog

URL = 'https://www.bilibili.com/video/local-test'
SECRET = 'SYNTHETIC_PRIVATE_VALUE'


def record(**changes):
    return {'domain': '.bilibili.com', 'name': 'SESSDATA', 'value': SECRET,
            'path': '/', 'secure': True, 'httpOnly': True, 'session': True, 'hostOnly': False, **changes}


class JsonCookieTests(unittest.TestCase):
    def test_common_export_shapes_session_httponly_and_scope(self):
        records = [record(), record(domain='.instagram.com', name='sessionid'),
                   record(name='old', session=False, expirationDate=1)]
        for data in (records, {'cookies': records}):
            inspection = inspect_cookie_text('\ufeff' + json.dumps(data), URL)
            self.assertEqual(inspection.format, 'JSON')
            self.assertEqual((len(inspection.jar), inspection.expired, inspection.unrelated), (1, 1, 1))
            cookie = next(iter(inspection.jar))
            self.assertIsNone(cookie.expires)
            self.assertTrue(cookie.has_nonstandard_attr('HttpOnly'))
            self.assertEqual(cookie.domain, '.bilibili.com')
            self.assertIn('SESSDATA=' + SECRET, inspection.jar.get_cookie_header(URL))
            self.assertFalse(inspection.jar.get_cookie_header('https://instagram.com/'))
            self.assertIn('已找到常见登录标记', inspection.summary())
            self.assertNotIn(SECRET, inspection.summary() + repr(inspection))

    def test_expiration_and_host_only_aliases_preserved(self):
        future = int(time.time()) + 3600
        data = [record(domain='www.bilibili.com', hostOnly=True, secure=False,
                       session=False, expires=future + 0.5)]
        cookie = next(iter(load_cookie_text(json.dumps(data), URL)))
        self.assertEqual(cookie.expires, future)
        self.assertFalse(cookie.domain_specified)
        self.assertFalse(cookie.secure)
        self.assertEqual(cookie.domain, 'www.bilibili.com')
        self.assertIsNone(next(iter(load_cookie_text(json.dumps([record(expires=-1)]), URL))).expires)

    def test_expired_wrong_site_and_missing_marker_have_different_guidance(self):
        for data, word in (([record(session=False, expirationDate=1)], '全部过期'),
                           ([record(domain='.instagram.com')], '当前网站')):
            with self.assertRaises(ValueError) as caught:
                load_cookie_text(json.dumps(data), URL)
            self.assertIn(word, str(caught.exception))
            self.assertNotIn(SECRET, str(caught.exception))
        report = inspect_cookie_text(json.dumps([record(name='visitor')]), URL)
        self.assertIn('未找到常见登录标记', report.summary())
        netscape = '# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t1\tSESSDATA\t' + SECRET + '\n'
        with self.assertRaisesRegex(ValueError, '全部过期'):
            load_cookie_text(netscape, URL)

    def test_malformed_records_and_partitioned_data_never_echo_values(self):
        cases = [json.dumps({'sessionid': SECRET}), '[{"value":"' + SECRET,
                 json.dumps([record(value=SECRET + '\r\n')]), json.dumps([record(secure='true')]),
                 json.dumps([record(domain='https://bilibili.com/')]), json.dumps([record(name='bad;name')]),
                 json.dumps([record(expirationDate=float('nan'))]), json.dumps([record(expirationDate=10**400)]),
                 json.dumps([record(partitionKey={'topLevelSite': URL})]),
                 json.dumps([record(session=False)]), json.dumps([record(value=None)]),
                 '[' * 2000 + ']' * 2000]
        for text in cases:
            with self.subTest(case=cases.index(text)):
                with self.assertRaises(ValueError) as caught:
                    load_cookie_text(text, URL)
                self.assertNotIn(SECRET, str(caught.exception))

    def test_known_site_aliases_match_without_cross_site_use(self):
        report = inspect_cookie_text(json.dumps([record(domain='.twitter.com', name='auth_token'),
                                                record(domain='.bilibili.com')]), 'https://x.com/test/status/1')
        self.assertEqual(len(report.jar), 1)
        self.assertEqual(report.unrelated, 1)
        self.assertIn('已找到常见登录标记', report.summary())


class LoginWorkflowTests(WindowTestCase):
    def setUp(self):
        super().setUp()
        self.window.url.setText(URL)

    def choose_json(self, data):
        path = self.root / 'export.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        with patch('video_demo.gui.QFileDialog.getOpenFileName', return_value=(str(path), '')):
            self.assertTrue(self.window.browse_cookies())
        return path

    def test_file_check_is_local_redacted_and_reloads_changed_file(self):
        path = self.choose_json([record(), record(domain='.instagram.com'),
                                 record(name='expired', session=False, expirationDate=1)])
        original = path.read_bytes()
        captured = []
        def close():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, HelpDialog)
            captured.append(dialog.content.toPlainText())
            dialog.accept()
        with patch('video_demo.gui.analyze') as network, patch('video_demo.gui.read_browser_session') as reader:
            QTimer.singleShot(0, close)
            self.window.cookie_check.click()
            network.assert_not_called()
            reader.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertIn('已过期 1 条', captured[0])
        self.assertIn('其他网站 1 条', captured[0])
        self.assertIn('JSON', captured[0])
        self.assertNotIn(SECRET, captured[0])
        path.write_text(json.dumps([record(session=False, expirationDate=1)]), encoding='utf-8')
        with patch('video_demo.gui.QMessageBox.information') as message:
            self.window.cookie_check.click()
        self.assertIn('全部过期', message.call_args.args[2])
        self.assertEqual(self.window.cookies.text(), str(path))
        self.assertIn('全部过期', self.window.cookie_status.text())

    def test_clipboard_json_rejects_bad_replacement_and_check_handles_site_change(self):
        self.window.accept_cookie_text(json.dumps([record()]))
        session = self.window.browser_session
        with self.assertRaises(ValueError):
            self.window.accept_cookie_text(json.dumps([record(domain='.instagram.com')]))
        self.assertIs(self.window.browser_session, session)
        self.window.url.setText('https://instagram.com/reel/local')
        with patch('video_demo.gui.QMessageBox.information') as message:
            self.window.cookie_check.click()
        self.assertIn('当前网站', message.call_args.args[2])
        self.assertIs(self.window.browser_session, session)
        self.assertNotIn(SECRET, self.window.log.toPlainText())

    def test_lookup_requires_explicit_button_and_is_deferred_until_settings_close(self):
        self.window.network_toggle.setChecked(True)
        self.window.accept_cookie_text(json.dumps([record()]))
        with patch.object(self.window, 'start_analysis') as lookup:
            self.app.processEvents()
            lookup.assert_not_called()
            self.window.settings_lookup.click()
            self.assertFalse(self.window.network_panel.isVisible())
            lookup.assert_not_called()
            self.app.processEvents()
            lookup.assert_called_once()
        self.window.url.clear()
        self.assertFalse(self.window.settings_lookup.isEnabled())
        worker = Worker('analyze', URL)
        self.window.worker = worker
        self.window.url.setText(URL)
        self.window.update_actions()
        self.assertFalse(self.window.settings_lookup.isEnabled())
        self.assertFalse(self.window.cookie_check.isEnabled())
        self.window.worker = None
        worker.deleteLater()

    def test_json_file_and_clipboard_reach_protected_local_download_and_decode(self):
        import threading
        make_fixtures()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                if 'session_json=synthetic' not in self.headers.get('Cookie', ''):
                    self.server.rejected += 1
                    self.send_error(401)
                    return
                self.server.authorized += 1
                if self.path == '/private.html':
                    body = b'<html><title>Local JSON session</title><video src="/first.mp4"></video></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    super().do_GET()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(FIXTURES)))
        server.rejected = server.authorized = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.window.url.setText(f'http://127.0.0.1:{server.server_port}/private.html')
            records = [record(domain='127.0.0.1', name='session_json', value='synthetic', secure=False, hostOnly=True),
                       record(domain='.bilibili.com')]
            with patch.dict(os.environ, {'NO_PROXY': 'localhost,127.0.0.1,::1'}):
                for mode in ('file', 'clipboard'):
                    with self.subTest(mode=mode):
                        path = None
                        if mode == 'file':
                            path = self.choose_json({'cookies': records})
                            original = path.read_bytes()
                        else:
                            self.window.accept_cookie_text(json.dumps(records))
                        self.window.network_toggle.setChecked(True)
                        self.window.settings_lookup.click()
                        self.wait(lambda: self.window.worker is not None)
                        self.wait(lambda: self.window.worker is None)
                        self.assertEqual(len(self.window.videos), 1)
                        self.window.folder.setText(str(self.root / mode))
                        with patch.object(self.window, 'check_previous_downloads', side_effect=lambda items: items):
                            self.window.start_download()
                        self.wait(lambda: self.window.worker is None)
                        self.assertEqual(self.window.states[0], '已完成')
                        inspect(Path(self.window.paths[0]))
                        if path:
                            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(server.rejected, 0)
            self.assertGreater(server.authorized, 3)
            self.assertNotIn(SECRET, self.window.log.toPlainText())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
