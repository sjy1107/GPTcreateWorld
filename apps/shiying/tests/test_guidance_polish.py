"""Task guidance, non-blocking history and cached preflight regressions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import functools
import http.server
import shutil
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QDialogButtonBox, QPlainTextEdit

from tests.integration_demo import FIXTURES, Handler, make_fixtures, inspect
from tests.test_stability import sample
from tests.test_workflow_additions import WindowTestCase
from video_demo.core import analyze, SafeYoutubeDL
from video_demo.gui import Worker
from video_demo.history_dialog import HistoryDialog
from video_demo.naming_dialog import NamingDialog
from video_demo.usage_guide import UsageGuide, guide_topics
from video_demo.transfer import estimate_space, estimate_sizes


class GuideTests(WindowTestCase):
    def test_all_feature_topics_search_empty_result_and_no_automatic_action(self):
        dialog = UsageGuide(self.window)
        try:
            topics = guide_topics()
            self.assertEqual(len({topic.key for topic in topics}), len(topics))
            self.assertTrue({'first', 'batch', 'quality', 'storage', 'naming', 'control', 'failure',
                             'resume', 'queue', 'login', 'history', 'cleanup', 'edge', 'proxy', 'terms', 'limits'} <= {topic.key for topic in topics})
            self.assertTrue(all(len(topic.content) > 70 for topic in topics))
            self.assertEqual(dialog.action, '')
            self.assertIn('不会下载', dialog.content.toPlainText())
            dialog.search.setText('eDgE')
            self.assertGreater(dialog.list.count(), 0)
            self.assertTrue(any(dialog.list.item(i).data(Qt.ItemDataRole.UserRole).key == 'edge' for i in range(dialog.list.count())))
            dialog.search.setText('not-a-topic-xxxxx')
            self.assertEqual(dialog.list.count(), 0)
            self.assertFalse(dialog.go.isEnabled())
            self.assertIn('没有找到', dialog.content.toPlainText())
            dialog.search.clear()
            self.assertEqual(dialog.list.count(), len(topics))
            dialog.select_topic('terms')
            self.assertFalse(dialog.go.isEnabled())
            self.assertIn('Cookies', dialog.content.toPlainText())
            self.assertEqual(dialog.action, '')
        finally:
            dialog.deleteLater()

    def test_context_explains_prerequisites_and_rechecks_before_navigation(self):
        dialog = UsageGuide(self.window, topic='naming', availability=self.window.guide_availability)
        try:
            self.assertFalse(dialog.go.isEnabled())
            self.assertIn('先在主界面', dialog.action_hint.text())
            self.window.url.setText('https://example.invalid/watch')
            self.window.populate([sample()])
            dialog.display_topic()
            self.assertTrue(dialog.go.isEnabled())
            worker = Worker('analyze', 'https://example.invalid/watch')
            self.window.worker = worker
            dialog.choose_action()
            self.assertEqual(dialog.action, '')
            self.assertIn('任务正在进行', dialog.action_hint.text())
            self.window.worker = None
            dialog.choose_action()
            self.assertEqual(dialog.action, 'naming')
            worker.deleteLater()
        finally:
            self.window.worker = None
            dialog.deleteLater()

    def test_every_navigation_route_opens_only_controls_and_never_starts_io_tasks(self):
        self.window.url.setText('https://example.invalid/watch')
        self.window.populate([sample()])
        self.window.states[0] = '失败'
        mapping = {'batch': 'show_batch_input', 'naming': 'show_naming', 'failure': 'show_failure',
                   'queue': 'show_queue', 'history': 'show_history', 'partials': 'show_progress_files'}
        with patch.object(self.window, 'start_worker') as started, \
                patch.object(self.window, 'start_cookie_read') as read, \
                patch.object(self.window, 'import_clipboard_cookies') as clipboard:
            for action, method in mapping.items():
                with patch.object(self.window, method) as opened:
                    self.window.run_guide_action(action)
                    opened.assert_called_once()
            for action in ('source', 'quality', 'storage', 'controls', 'login', 'proxy'):
                self.window.run_guide_action(action)
            for action, tab in (('health', 0), ('data', 1)):
                with patch.object(self.window, 'show_system') as opened:
                    self.window.run_guide_action(action)
                    opened.assert_called_once_with(tab=tab)
            self.assertEqual(self.window.login_tabs.currentIndex(), 1)
            self.assertTrue(self.window.proxy_toggle.isChecked())
            started.assert_not_called()
            read.assert_not_called()
            clipboard.assert_not_called()
            self.assertEqual(self.window.url.text(), 'https://example.invalid/watch')

    def test_modal_action_runs_after_guide_closes_and_is_cancelled_with_owner(self):
        def click_topic():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, UsageGuide)
            dialog.select_topic('history')
            dialog.go.click()
        with patch.object(self.window, 'show_history') as history:
            QTimer.singleShot(0, click_topic)
            self.window.show_quick_help()
            history.assert_not_called()
            self.app.processEvents()
            history.assert_called_once()
        other = self.new_window()
        QTimer.singleShot(0, click_topic)
        with patch.object(other, 'show_history') as history:
            other.show_quick_help()
            other.close()
            other.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
            history.assert_not_called()
        self.windows.remove(other)

    def test_website_title_remains_plain_text(self):
        title = '<b>原始视频标题</b><img src="local-file">'
        self.window.populate([sample()])
        self.window.handle_event('current', {'row': 0, 'text': title})
        self.assertEqual(self.window.task_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.window.task_label.text(), title)


class CachedPreflightTests(WindowTestCase):
    def test_first_start_estimates_once_and_second_uses_cache_without_format_selection(self):
        self.window.url.setText('https://example.invalid/watch')
        self.window.populate([sample(0), sample(1)])
        self.window.folder.setText(str(self.root / 'outputs'))
        # Stop the optional delayed hint; count only the actual button flow.
        self.window.size_timer.stop()
        self.window.size_cache.clear()
        with patch('video_demo.gui.estimate_sizes', wraps=estimate_sizes) as estimate, \
                patch('video_demo.transfer.estimate_sizes', side_effect=AssertionError('preflight repeated format selection')), \
                patch.object(self.window, 'start_worker') as started:
            self.window.start_download()
            self.assertEqual(estimate.call_count, 1)
            self.window.start_download()
            self.assertEqual(estimate.call_count, 1)
            self.assertEqual(started.call_count, 2)

    def test_changing_quality_invalidates_only_the_new_selection(self):
        self.window.url.setText('https://example.invalid/watch')
        video = sample()
        video.info['formats'].append({**video.info['formats'][0], 'format_id': '360', 'height': 360, 'filesize': 1000})
        video.heights = [720, 360]
        self.window.populate([video])
        self.window.folder.setText(str(self.root))
        self.window.size_timer.stop()
        with patch.object(self.window, 'start_worker'), patch('video_demo.gui.estimate_sizes', wraps=estimate_sizes) as estimate:
            self.window.start_download()
            combo = self.window.table.cellWidget(0, 3)
            combo.setCurrentIndex(combo.findData(360))
            self.window.size_timer.stop()
            self.window.start_download()
            self.assertEqual(estimate.call_count, 2)
            self.assertEqual(estimate.call_args.args[0][0][2], 360)


class BackgroundHistoryTests(WindowTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        make_fixtures()

    def download_setup(self):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(FIXTURES)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/first.mp4'
        videos = analyze(url, threading.Event(), lambda *_: None)
        self.window.url.setText(url)
        self.window.populate(videos)
        self.window.folder.setText(str(self.root / 'outputs'))
        return server

    def test_slow_history_write_is_off_gui_thread_and_does_not_duplicate_records(self):
        server = self.download_setup()
        main_thread = threading.get_ident()
        threads, ticks = [], []
        writing = threading.Event()
        original = self.window.history.record
        def slow_record(*args):
            threads.append(threading.get_ident())
            writing.set()
            time.sleep(0.3)
            return original(*args)
        timer = QTimer(self.window)
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1) if writing.is_set() else None)
        timer.start()
        try:
            with patch.object(self.window.history, 'record', side_effect=slow_record) as record:
                self.window.start_download()
                guide_seen, guide_errors = [], []
                def inspect_guide():
                    dialog = QApplication.activeModalWidget()
                    try:
                        self.assertIsInstance(dialog, UsageGuide)
                        dialog.select_topic('naming')
                        self.assertFalse(dialog.go.isEnabled())
                        guide_seen.append(dialog)
                    except Exception as exc:
                        guide_errors.append(str(exc))
                def close_after_finished():
                    if self.window.worker is None and guide_seen:
                        dialog = guide_seen[0]
                        if not dialog.go.isEnabled():
                            guide_errors.append('Guide context did not update after worker finished')
                        dialog.reject()
                closer = QTimer(self.window)
                closer.setInterval(10)
                closer.timeout.connect(close_after_finished)
                closer.start()
                QTimer.singleShot(0, inspect_guide)
                # The guide remains readable while real download/history work
                # runs, and its actions become available without reopening it.
                safety = QTimer(self.window)
                safety.setSingleShot(True)
                safety.timeout.connect(lambda: QApplication.activeModalWidget().reject() if QApplication.activeModalWidget() else None)
                safety.start(15000)
                self.window.show_quick_help(topic='naming')
                closer.stop()
                safety.stop()
                self.wait(lambda: self.window.worker is None)
                self.assertEqual(record.call_count, 1)
                self.assertTrue(guide_seen)
                self.assertFalse(guide_errors, guide_errors)
            self.assertEqual(len(threads), 1)
            self.assertNotEqual(threads[0], main_thread)
            self.assertGreater(len(ticks), 5)
            self.assertEqual(self.window.states[0], '已完成')
            self.assertEqual(len(self.window.history.recent()), 1)
            inspect(Path(self.window.paths[0]))
        finally:
            timer.stop()
            server.shutdown()
            server.server_close()

    def test_guided_batch_naming_download_and_history_real_qt_workflow(self):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(FIXTURES)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        callback_errors = []
        def via_guide(topic, child_action):
            def choose():
                dialog = QApplication.activeModalWidget()
                if isinstance(dialog, UsageGuide):
                    dialog.select_topic(topic)
                    dialog.go.click()
            def in_child():
                try:
                    child_action(QApplication.activeModalWidget())
                except Exception as exc:
                    callback_errors.append(repr(exc))
                    if QApplication.activeModalWidget():
                        QApplication.activeModalWidget().reject()
            QTimer.singleShot(0, choose)
            self.window.show_quick_help(topic=topic)
            QTimer.singleShot(0, in_child)
            self.app.processEvents()
        try:
            def enter_batch(dialog):
                editor = dialog.findChild(QPlainTextEdit)
                self.assertIsNotNone(editor)
                editor.setPlainText(base + '/first.mp4\n' + base + '/second.webm')
                next(button for button in dialog.findChild(QDialogButtonBox).buttons() if button.text() == '查找这些链接').click()
            via_guide('batch', enter_batch)
            self.wait(lambda: self.window.worker is None)
            self.assertEqual(len(self.window.videos), 2)
            def name_files(dialog):
                self.assertIsInstance(dialog, NamingDialog)
                dialog.table.item(0, 1).setText('指南练习一')
                dialog.table.item(1, 1).setText('指南练习二')
                dialog.accept()
            via_guide('naming', name_files)
            self.assertIsNone(self.window.worker)  # Opening guidance does not download.
            self.window.folder.setText(str(self.root / 'guided outputs'))
            self.window.start_download()
            self.wait(lambda: self.window.worker is None)
            self.assertEqual(self.window.states, {0: '已完成', 1: '已完成'})
            self.assertEqual({Path(path).name for path in self.window.paths.values()}, {'指南练习一.mp4', '指南练习二.mp4'})
            for path in self.window.paths.values():
                inspect(Path(path))
            def read_history(dialog):
                self.assertIsInstance(dialog, HistoryDialog)
                self.assertEqual(dialog.table.rowCount(), 2)
                dialog.accept()
            via_guide('history', read_history)
            self.assertFalse(callback_errors, callback_errors)
        finally:
            if self.window.worker:
                self.window.worker.cancel.set()
                self.window.worker.wait(15000)
                self.app.processEvents()
            server.shutdown()
            server.server_close()

    def test_history_failure_keeps_completed_mp4_and_is_not_retried_by_ui(self):
        server = self.download_setup()
        try:
            with patch.object(self.window.history, 'record', side_effect=sqlite3.OperationalError('locked')) as record:
                self.window.start_download()
                self.wait(lambda: self.window.worker is None)
            self.assertEqual(record.call_count, 1)
            self.assertEqual(self.window.states[0], '已完成')
            self.assertIn('历史暂时写入失败', self.window.log.toPlainText())
            inspect(Path(self.window.paths[0]))
        finally:
            server.shutdown()
            server.server_close()

    def test_unknown_upstream_hook_statuses_do_not_break_actual_download(self):
        server = self.download_setup()
        original = SafeYoutubeDL.process_ie_result
        seen = []
        def with_future_statuses(ydl, info, *args, **kwargs):
            for key in ('progress_hooks', 'postprocessor_hooks'):
                for callback in ydl.params.get(key, []):
                    callback({})
                    callback({'status': 'future-upstream-state'})
                    seen.append(key)
            return original(ydl, info, *args, **kwargs)
        try:
            with patch.object(SafeYoutubeDL, 'process_ie_result', new=with_future_statuses):
                self.window.start_download()
                self.wait(lambda: self.window.worker is None)
            self.assertEqual(set(seen), {'progress_hooks', 'postprocessor_hooks'})
            self.assertEqual(self.window.states[0], '已完成')
            inspect(Path(self.window.paths[0]))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
