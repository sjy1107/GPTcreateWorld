"""One continuous user journey across dialogs, interruption and window restart."""
import http.server
import json
import threading
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox, QPlainTextEdit

from tests.integration_demo import FIXTURES, inspect, make_fixtures
from tests.test_resume import RangeHandler
from tests.test_workflow_additions import WindowTestCase
from video_demo.history_dialog import HistoryDialog
from video_demo.core import analyze
from video_demo.naming_dialog import NamingDialog
from video_demo.queue_dialog import QueueDialog
from video_demo.resume import list_progress

REPORT = Path(__file__).with_name('acceptance_report_20261018.json')


class AcceptanceFlowTests(WindowTestCase):
    def test_share_batch_naming_skip_close_restore_resume_history_and_cleanup(self):
        make_fixtures()
        class Handler(RangeHandler):
            def do_GET(self):
                if self.path.endswith('.html'):
                    self.server.pages.append(self.path)
                    if self.path == '/missing.html':
                        self.send_error(404)
                        return
                    files = ['/first.mp4', '/second.mp4'] if self.path == '/a.html' else ['/third.mp4']
                    body = ('<html><title>Acceptance synthetic fixtures</title>' + ''.join(
                        f'<video src="{name}"></video>' for name in files) + '</html>').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    super().do_GET()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.payload = (FIXTURES / 'first.mp4').read_bytes()
        server.etag, server.supports_range, server.change_during_resume = '"acceptance-v1"', True, False
        server.assets, server.tags, server.requests, server.pages = {}, {}, [], []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        checks, callback_errors, ui_errors = {}, [], []
        report = {'date': '2026-10-18', 'scope': 'Continuous Qt workflow with local synthetic media only',
                  'status': 'running', 'checks': checks, 'external_requests': False, 'real_browser_credentials': False}
        def save():
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        def schedule(callback):
            def action():
                dialog = QApplication.activeModalWidget()
                try:
                    callback(dialog)
                except BaseException as exc:
                    callback_errors.append(type(exc).__name__ + ': ' + str(exc))
                    if dialog:
                        dialog.reject()
            QTimer.singleShot(10, action)
        base = f'http://127.0.0.1:{server.server_port}'
        urls = [base + '/a.html', base + '/missing.html', base + '/b.html']
        self.window.error_dialog = lambda *args: ui_errors.append(str(args[:2]))
        self.window.show()
        save()
        try:
            QApplication.clipboard().setText('分享第一组：' + urls[0] + '。\n第二组 ' + urls[1] + '\n最后一组 ' + urls[2])
            def submit(dialog):
                self.assertEqual(dialog.findChild(QPlainTextEdit).toPlainText(), '\n'.join(urls))
                next(button for button in dialog.findChild(QDialogButtonBox).buttons() if button.text() == '查找这些链接').click()
            schedule(submit)
            self.window.paste_button.click()
            self.wait(lambda: self.window.worker is None)
            self.assertFalse(callback_errors, callback_errors)
            self.assertFalse(ui_errors, ui_errors)
            self.assertEqual(len(self.window.videos), 3)
            self.assertEqual(self.window.failed_urls, [urls[1]])
            checks['share_text_batch_confirmation_partial_failure_and_three_results'] = 'passed'
            def inspect_retry(dialog):
                self.assertEqual(dialog.findChild(QPlainTextEdit).toPlainText(), urls[1])
                dialog.reject()
            schedule(inspect_retry)
            self.window.batch_button.click()
            checks['failed_link_prefill_without_replacing_results'] = 'passed'
            def name(dialog):
                self.assertIsInstance(dialog, NamingDialog)
                for row in range(3):
                    dialog.table.item(row, 1).setText('验收收藏.mp4')
                self.assertEqual(dialog.table.item(0, 2).text(), '验收收藏.mp4')
                dialog.accept()
            schedule(name)
            self.window.naming_button.click()
            folder = self.root / 'outputs'
            self.window.folder.setText(str(folder))
            self.window.rate_limit.setCurrentIndex(self.window.rate_limit.findData(512 * 1024))
            self.window.set_queue_memory(True)
            flags = {'skip': False, 'close': False, 'speed': False}
            original = self.window.handle_event
            def event(kind, data):
                original(kind, data)
                if kind == 'progress' and data.get('speed'):
                    flags['speed'] = True
                if kind == 'progress' and data.get('phase') == '下载' and (data.get('percent') or 0) > 20:
                    if self.window.current_row == 0 and not flags['skip']:
                        flags['skip'] = True
                        self.window.skip_button.click()
                    elif self.window.current_row == 1 and not flags['close']:
                        flags['close'] = True
                        self.window.close()
            self.window.handle_event = event
            self.window.download_button.click()
            self.wait(lambda: self.window.worker is None)
            self.assertTrue(all(flags.values()), flags)
            self.assertFalse(self.window.isVisible())
            self.assertEqual(self.window.states, {0: '已跳过', 1: '已取消', 2: '已取消'})
            self.assertEqual(len(list_progress(folder)), 2)
            self.assertEqual(len(self.window.queue_store.snapshot['items']), 3)
            checks['live_speed_skip_then_close_during_next_transfer'] = 'passed'
            checks['close_keeps_queue_names_and_two_partial_transfers'] = 'passed'
            save()
            fresh = self.new_window()
            fresh.error_dialog = lambda *args: ui_errors.append(str(args[:2]))
            fresh.show()
            self.assertFalse(fresh.videos)
            self.assertIsNone(fresh.worker)
            server.pages.clear()
            with patch('video_demo.gui.analyze', wraps=analyze) as lookup:
                self.assertTrue(fresh.begin_queue_restore())
                self.wait(lambda: fresh.worker is None)
            self.assertEqual([call.args[0] for call in lookup.call_args_list], [urls[0], urls[2]])
            self.assertEqual(fresh.custom_names, {0: '验收收藏.mp4', 1: '验收收藏.mp4', 2: '验收收藏.mp4'})
            self.assertEqual(fresh.folder.text(), str(folder))
            self.assertFalse(list(folder.glob('*.mp4')))
            checks['restart_restore_groups_pages_restores_names_folder_no_auto_download'] = 'passed'
            queue = QueueDialog(fresh)
            queue.table.selectRow(2)
            queue.remove.click()
            self.assertEqual(fresh.selected_rows(), [0, 1])
            self.assertEqual(len(fresh.queue_store.snapshot['items']), 2)
            queue.deleteLater()
            checks['selective_queue_removal_updates_download_selection'] = 'passed'
            reserved = folder / '验收收藏.mp4'
            reserved.write_bytes(server.payload)
            original_bytes = reserved.read_bytes()
            server.requests.clear()
            fresh.download_button.click()
            self.wait(lambda: fresh.worker is None)
            self.assertFalse(ui_errors, ui_errors)
            self.assertEqual([fresh.states[index] for index in (0, 1)], ['已完成', '已完成'])
            resumed_paths = {item['path'] for item in server.requests if item['if_range'] and item['range'] != 'bytes=0-0'}
            self.assertTrue({'/first.mp4', '/second.mp4'} <= resumed_paths, server.requests)
            self.assertNotIn('/third.mp4', {item['path'] for item in server.requests})
            self.assertEqual(reserved.read_bytes(), original_bytes)
            self.assertEqual({Path(path).name for path in fresh.paths.values()}, {'验收收藏 (2).mp4', '验收收藏 (3).mp4'})
            for path in fresh.paths.values():
                inspect(Path(path))
            fresh.persist_queue()
            self.assertFalse(fresh.queue_store.snapshot['items'])
            self.assertFalse(list_progress(folder))
            checks['two_verified_range_resumes_final_mp4_decode_and_no_overwrite'] = 'passed'
            checks['success_clears_queue_and_partial_material'] = 'passed'
            for row in (0, 1):
                fresh.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
            def reuse(dialog):
                self.assertIsInstance(dialog, QMessageBox)
                next(button for button in dialog.buttons() if button.text() == '使用已有文件').click()
            schedule(reuse)
            server.requests.clear()
            with patch.object(fresh, 'start_worker') as started:
                fresh.download_button.click()
                started.assert_not_called()
            self.assertFalse(server.requests)
            self.assertEqual(len(fresh.history.recent()), 2)
            checks['history_reuse_avoids_network_and_duplicate_records'] = 'passed'
            first = Path(fresh.paths[0])
            first.unlink()
            history = HistoryDialog(fresh.history, fresh)
            history.check_button.click()
            self.assertTrue(any(history.table.item(row, 4).text() == '文件找不到' for row in range(history.table.rowCount())))
            with patch('video_demo.history_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                history.prune_button.click()
            self.assertEqual(len(fresh.history.recent()), 1)
            self.assertTrue(Path(fresh.paths[1]).is_file())
            history.deleteLater()
            checks['history_missing_file_detection_cleanup_keeps_other_output'] = 'passed'
            self.assertFalse(callback_errors, callback_errors)
            report['status'] = 'passed'
        except BaseException as exc:
            report['status'] = 'failed'
            report['failure'] = type(exc).__name__ + ': ' + str(exc)[:500]
            raise
        finally:
            save()
            for window in self.windows:
                if window.worker:
                    window.worker.cancel.set()
                    window.worker.wait(15000)
                    self.app.processEvents()
            server.shutdown()
            server.server_close()
            thread.join(3)
