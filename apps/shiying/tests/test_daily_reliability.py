"""Regression checks for local records, task removal and optional login use."""
import concurrent.futures
import functools
import http.server
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from PySide6.QtCore import QItemSelectionModel, QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.integration_demo import FIXTURES, inspect, make_fixtures
from tests.test_queue import job
from tests.test_transfer import video
from tests.test_workflow_additions import WindowTestCase
from video_demo import preferences
from video_demo.diagnostics import DiagnosticDialog
from video_demo.errors import explain_error
from video_demo.history import file_state
from video_demo.history_dialog import HistoryDialog
from video_demo.queue_dialog import QueueDialog
from video_demo.queue_store import QueueStore


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_preferences_concurrent_writes_and_failed_replace_preserve_last_good_file(self):
        path = self.root / 'preferences.json'
        folders = [self.root / str(index) for index in range(8)]
        with patch('video_demo.preferences.PREFERENCES', path):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(preferences.save_folder, folders))
            self.assertIn(preferences.load_folder(self.root), {str(folder) for folder in folders})
            before = path.read_bytes()
            with patch('video_demo.preferences.os.replace', side_effect=PermissionError('busy')):
                with self.assertRaises(OSError):
                    preferences.save_folder(self.root / 'uncommitted')
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(self.root.glob('.preferences-*.tmp')))

    def test_deep_oversized_invalid_and_unicode_preferences_have_safe_boundaries(self):
        path = self.root / 'preferences.json'
        with patch('video_demo.preferences.PREFERENCES', path):
            for raw in (b'[' * 2000 + b']' * 2000, b'x' * (preferences.MAX_BYTES + 1),
                        b'{"folder":null}', b'{"folder":"bad\\u0000path"}', b'[]', b'broken'):
                path.write_bytes(raw)
                self.assertEqual(preferences.load_folder(self.root), str(self.root))
            text = '\U0001f30a' * 32768
            preferences.save_folder(text)
            self.assertEqual(preferences.load_folder(self.root), text)

    def test_queue_deep_records_and_oversized_save_do_not_destroy_previous_queue(self):
        path = self.root / 'queue.json'
        path.write_bytes(b'[' * 2000 + b']' * 2000)
        loaded = QueueStore(path)
        self.assertTrue(loaded.load_error)
        loaded.enabled = True
        loaded.save([job(video())], self.root)
        before = path.read_bytes()
        jobs = []
        for index in range(50):
            item = job(video(str(index)))
            item['url'] = 'https://example.com/' + '字' * 7800 + f'?id={index}'
            jobs.append(item)
        with self.assertRaisesRegex(ValueError, '过大'):
            loaded.save(jobs, self.root)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(QueueStore(path).snapshot['items']), 1)

    def test_queue_remove_atomic_failure_keeps_items_and_naming(self):
        store = QueueStore(self.root / 'queue.json')
        store.enabled = True
        store.save([job(video('one')), job(video('two'))], self.root, naming_mode='title')
        before = store.path.read_bytes()
        with patch('video_demo.queue_store.os.replace', side_effect=PermissionError('busy')):
            with self.assertRaises(OSError):
                store.remove_items({0})
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(len(store.snapshot['items']), 2)
        with self.assertRaises(ValueError):
            store.remove_items({True})
        store.remove_items({0})
        reopened = QueueStore(store.path)
        self.assertTrue(reopened.enabled)
        self.assertEqual(reopened.snapshot['naming_mode'], 'title')
        self.assertEqual(reopened.snapshot['items'], [job(video('two'))])


class QueueManagementTests(WindowTestCase):
    def setUp(self):
        super().setUp()
        self.window.url.setText('https://www.bilibili.com/video/one')
        self.window.populate([video('one'), video('two'), video('three')])
        self.window.folder.setText(str(self.root / 'outputs'))
        self.window.naming_mode = 'title'
        self.window.custom_names = {0: 'keep-name', 1: 'remove-name'}
        self.window.set_queue_memory(True)

    def test_partial_restore_then_multi_remove_does_not_reappear_after_restart(self):
        fresh = self.new_window()
        with patch('video_demo.gui.analyze', return_value=[video('one'), video('two')]):
            fresh.begin_queue_restore()
            self.wait(lambda: fresh.worker is None)
        self.assertEqual(len(fresh.queue_unresolved), 1)
        saved_video = self.root / 'completed.mp4'
        saved_video.write_bytes(b'untouched output')
        dialog = QueueDialog(fresh)
        try:
            # Unresolved jobs are saved first; remove it plus restored 'two'.
            # Match stored identities rather than potentially equal titles.
            keep_identity = job(video('one'))['identity']
            rows = [index for index, item in enumerate(fresh.queue_store.snapshot['items']) if item['identity'] != keep_identity]
            for row in rows:
                dialog.table.selectionModel().select(dialog.table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
            self.assertTrue(dialog.remove.isEnabled())
            dialog.remove.click()
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(fresh.selected_rows(), [0])
            self.assertFalse(fresh.queue_unresolved)
            fresh.persist_queue()
            fresh.close()
            reopened = self.new_window()
            snapshot = reopened.queue_store.snapshot
            self.assertEqual(len(snapshot['items']), 1)
            self.assertEqual(snapshot['items'][0]['identity'], keep_identity)
            self.assertEqual(snapshot['items'][0]['filename'], 'keep-name')
            self.assertEqual(snapshot['naming_mode'], 'title')
            self.assertEqual(saved_video.read_bytes(), b'untouched output')
        finally:
            dialog.deleteLater()
        # The original window predates the deletion; don't let its close save
        # its intentionally independent, stale copy over this test's snapshot.
        self.window.queue_store.enabled = False

    def test_removal_before_restore_and_write_failure_do_not_uncheck_wrong_rows(self):
        fresh = self.new_window()
        self.assertFalse(fresh.videos)
        self.assertTrue(fresh.remove_queue_items({1}))
        fresh.close()
        self.assertEqual(len(QueueStore(fresh.queue_store.path).snapshot['items']), 2)
        before = self.window.queue_store.path.read_bytes()
        with patch('video_demo.queue_store.os.replace', side_effect=PermissionError('busy')):
            self.assertFalse(self.window.remove_queue_items({0}))
        self.assertEqual(self.window.selected_rows(), [0, 1, 2])
        self.assertEqual(self.window.queue_store.path.read_bytes(), before)
        self.window.queue_store.enabled = False


class HistoryMaintenanceTests(WindowTestCase):
    def setUp(self):
        super().setUp()
        self.paths = {}
        for index, label in enumerate(('正常', '缺失', '变化', '暂不可读', '其他')):
            path = self.root / (label + '.mp4')
            path.write_bytes(b'synthetic original content')
            item = video(str(index))
            item.title = ('其他' if label == '其他' else '旅行') + ' · ' + label
            self.window.history.record(item, None, path)
            self.paths[label] = path
        self.paths['缺失'].unlink()
        self.paths['变化'].write_bytes(b'edited')
        self.paths['其他'].unlink()

    def test_refresh_status_search_avoids_repeated_disk_work_and_bad_date_is_readable(self):
        with closing(sqlite3.connect(self.window.history.path)) as db, db:
            db.execute("UPDATE downloads SET finished='bad timestamp' WHERE title LIKE '%正常'")
        dialog = HistoryDialog(self.window.history, self.window)
        try:
            self.assertTrue(any(dialog.table.item(row, 2).text() == '时间未知' for row in range(dialog.table.rowCount())))
            dialog.check_button.click()
            states = {dialog.table.item(row, 4).text() for row in range(dialog.table.rowCount())}
            self.assertTrue({'文件在原位置', '文件找不到', '文件大小已变化'} <= states)
            with patch('video_demo.history_dialog.file_state') as checked:
                dialog.search.setText('旅行')
                dialog.search.clear()
                checked.assert_not_called()
            dialog.search.setText(str(self.paths['正常']))
            self.assertEqual(dialog.table.rowCount(), 1)
        finally:
            dialog.deleteLater()

    def test_filtered_cleanup_keeps_other_records_unreadable_and_every_existing_file(self):
        dialog = HistoryDialog(self.window.history, self.window)
        def status(record):
            return 'unreadable' if record['path'] == str(self.paths['暂不可读']) else file_state(record)
        try:
            dialog.search.setText('旅行')
            with patch('video_demo.history_dialog.file_state', side_effect=status):
                with patch('video_demo.history_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
                    dialog.prune_button.click()
                self.assertEqual(len(self.window.history.recent()), 5)
                with patch('video_demo.history_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                    dialog.prune_button.click()
            titles = {record['title'] for record in self.window.history.recent()}
            self.assertEqual(titles, {'旅行 · 正常', '旅行 · 暂不可读', '其他 · 其他'})
            self.assertEqual(self.paths['变化'].read_bytes(), b'edited')
            self.assertEqual(self.paths['正常'].read_bytes(), b'synthetic original content')
        finally:
            dialog.deleteLater()

    def test_player_failure_and_changed_file_do_not_silently_play(self):
        dialog = HistoryDialog(self.window.history, self.window)
        try:
            dialog.search.setText('正常')
            dialog.table.selectRow(0)
            with patch('video_demo.history_dialog.QDesktopServices.openUrl', return_value=False), patch('video_demo.history_dialog.QMessageBox.information') as info:
                dialog.open_file()
                self.assertIn('播放器', info.call_args.args[1])
            dialog.search.setText('变化')
            dialog.table.selectRow(0)
            with patch('video_demo.history_dialog.QDesktopServices.openUrl') as opened, patch('video_demo.history_dialog.QMessageBox.information') as info:
                dialog.open_file()
                opened.assert_not_called()
                self.assertIn('变化', info.call_args.args[1])
        finally:
            dialog.deleteLater()


class PublicAccessTests(WindowTestCase):
    def test_public_retry_is_explicit_retains_login_and_scopes_download_retry(self):
        self.window.url.setText('https://www.bilibili.com/video/one')
        self.window.populate([video('one'), video('two')])
        self.window.accept_cookie_text('# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic\n')
        session = self.window.browser_session
        hint = explain_error(RuntimeError('HTTP Error 403: Forbidden'))
        self.window.error_context = {'hint': hint, 'operation': 'download', 'url': self.window.url.text(), 'row': 1}
        def choose():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, DiagnosticDialog)
            dialog.action_buttons['public'].click()
        QTimer.singleShot(0, choose)
        with patch.object(self.window, 'start_download') as retry:
            self.window.error_dialog(hint.title, hint.as_text())
            retry.assert_not_called()
            self.app.processEvents()
            retry.assert_called_once_with(retry_only=True, only_rows={1})
        self.assertFalse(self.window.use_login.isChecked())
        self.assertIs(self.window.browser_session, session)
        self.assertIn('暂不使用', self.window.source_hint.text())
        for error in ('This video is DRM protected', 'This video is only available for premium members'):
            protected = explain_error(RuntimeError(error))
            dialog = DiagnosticDialog(protected.title, protected.as_text(), protected, '', 'download', self.window, allow_public=True)
            self.assertNotIn('public', dialog.action_buttons)
            dialog.deleteLater()

    def test_real_public_download_without_cookies_then_restore_private_session_and_missing_file_fallback(self):
        make_fixtures()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                cookie = self.headers.get('Cookie', '')
                self.server.requests.append((self.path, cookie))
                authorized = not cookie if self.server.mode == 'public' else 'session_saved=synthetic' in cookie
                if not authorized:
                    self.send_error(403)
                    return
                if self.path.endswith('.html'):
                    body = b'<html><title>Access test</title><video src="/first.mp4"></video></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    super().do_GET()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(FIXTURES)))
        server.mode, server.requests = 'public', []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        self.window.error_dialog = lambda *args: errors.append(args)
        url = f'http://127.0.0.1:{server.server_port}/public.html'
        def run_round():
            self.window.start_analysis()
            self.wait(lambda: self.window.worker is None)
            self.assertFalse(errors, errors)
            self.assertEqual(len(self.window.videos), 1)
            self.window.folder.setText(str(self.root / 'outputs'))
            with patch.object(self.window, 'check_previous_downloads', side_effect=lambda items: items):
                self.window.start_download()
            self.wait(lambda: self.window.worker is None)
            self.assertEqual(self.window.states[0], '已完成')
            inspect(Path(self.window.paths[0]))
        try:
            self.window.url.setText(url)
            self.window.accept_cookie_text('# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tsession_saved\tsynthetic\n')
            session = self.window.browser_session
            self.window.use_login.setChecked(False)
            run_round()
            self.assertTrue(server.requests)
            self.assertTrue(all(not cookie for _, cookie in server.requests))
            self.assertIs(self.window.browser_session, session)
            server.requests.clear()
            server.mode = 'private'
            self.window.use_login.setChecked(True)
            self.window.url.setText(url.replace('public.html', 'private.html'))
            run_round()
            self.assertTrue(all('session_saved=synthetic' in cookie for _, cookie in server.requests))
            self.window.clear_login_information()
            missing = str(self.root / 'moved-cookies.json')
            self.window.cookies.setText(missing)
            self.window.use_login.setChecked(True)
            with self.assertRaises(ValueError):
                self.window.network_settings()
            self.window.use_login.setChecked(False)
            server.mode, server.requests = 'public', []
            self.window.url.setText(url)
            run_round()
            self.assertEqual(self.window.cookies.text(), missing)
            self.assertTrue(all(not cookie for _, cookie in server.requests))
            self.assertEqual(len(list((self.root / 'outputs').glob('*.mp4'))), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
