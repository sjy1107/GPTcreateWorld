"""Offline launch checks, read-only inventory and exact-scope data management."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import io
import json
import shutil
import sqlite3
import tempfile
import time
import subprocess
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.test_workflow_additions import WindowTestCase
from tests.test_storage_management import progress_fixture
from tests.test_stability import sample
from video_demo import bootstrap, preferences
from video_demo.data_inventory import clear_record_file, inventory, output_summary
from video_demo.health import Check, environment_checks, history_count, runtime_check
from video_demo.system_dialog import SystemDialog


class BootstrapTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_launcher_missing_venv_explains_setup_without_running_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(__file__).resolve().parent.parent / 'start.bat'
            shutil.copyfile(source, root / 'start.bat')
            result = subprocess.run(['cmd', '/d', '/c', 'start.bat'], cwd=root, input=b'\r\n', capture_output=True, timeout=6)
            self.assertEqual(result.returncode, 1)
            self.assertIn('python -m venv', result.stdout.decode('utf-8', 'replace'))
            self.assertFalse((root / '.venv').exists())

    def test_missing_dependencies_produce_readable_offline_failure_and_log(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(bootstrap, 'ROOT', Path(temp)), \
                patch('video_demo.bootstrap.importlib.util.find_spec', return_value=None), patch('sys.stderr', new_callable=io.StringIO) as stream:
            self.assertEqual(bootstrap.launch(popup=False), 1)
            self.assertIn('界面组件未安装', stream.getvalue())
            self.assertIn('start.bat', stream.getvalue())
            self.assertIn('pip install', stream.getvalue())
            self.assertTrue((Path(temp) / '.local/startup.log').exists())

    def test_old_python_and_initialization_error_do_not_need_qt_error_ui(self):
        with patch('video_demo.bootstrap.sys.version_info', (3, 11, 0)):
            self.assertTrue(any('版本过旧' in item for item in bootstrap.dependency_issues()))
        with tempfile.TemporaryDirectory() as temp, patch.object(bootstrap, 'ROOT', Path(temp)), \
                patch('video_demo.bootstrap.dependency_issues', return_value=[]), \
                patch('video_demo.gui.main', side_effect=RuntimeError('private_url_or_cookie_secret')), \
                patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(bootstrap.launch(popup=False), 1)
            message = (Path(temp) / '.local/startup.log').read_text(encoding='utf-8')
            self.assertIn('RuntimeError', message)
            self.assertNotIn('private_url_or_cookie_secret', message)

    def test_import_diagnostics_keep_component_and_frames_without_arbitrary_secrets(self):
        try:
            raise ImportError('DLL load failed while importing QtWidgets: 找不到指定的程序。', name='QtWidgets')
        except ImportError as error:
            detail = bootstrap.failure_details(error)
        self.assertIn('QtWidgets', detail)
        self.assertIn('找不到指定的程序', detail)
        self.assertIn('test_system_management.py', detail)
        self.assertNotIn('private_cookie_secret', bootstrap.failure_details(ImportError('private_cookie_secret')))
        self.assertNotIn('private_cookie_secret', bootstrap.failure_details(RuntimeError('private_cookie_secret')))


class HealthTests(unittest.TestCase):
    def test_script_runtime_checks_full_upstream_version_not_only_major(self):
        with patch('video_demo.core.js_runtimes', return_value={}), patch('video_demo.health.shutil.which', return_value='deno.exe'):
            with patch('video_demo.health.run_program', return_value=subprocess.CompletedProcess([], 0, b'deno 2.2.0\n', b'')):
                self.assertEqual(runtime_check().state, 'warning')
            with patch('video_demo.health.run_program', return_value=subprocess.CompletedProcess([], 0, b'deno 2.3.0\n', b'')):
                self.assertEqual(runtime_check().state, 'ok')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'outputs'
        self.folder.mkdir()
        self.prefs = self.root / '.local/preferences.json'
        self.history = self.root / '.local/history.sqlite3'
        self.queue = self.root / '.local/queue.json'

    def tearDown(self):
        self.temp.cleanup()

    def checks(self, **kwargs):
        return environment_checks(self.folder, self.prefs, self.history, self.queue, **kwargs)

    def test_real_ffmpeg_encoder_probe_and_no_first_run_record_creation_or_network(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline check used network')):
            rows = {item.key: item for item in self.checks()}
        self.assertEqual(rows['ffmpeg'].state, 'ok')
        self.assertIn('AAC', rows['ffmpeg'].detail)
        self.assertEqual(rows['folder'].state, 'ok')
        self.assertFalse(self.prefs.parent.exists())
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(rows['history'].state, 'ok')

    def test_failed_ffmpeg_and_corrupt_records_report_without_changing_files(self):
        self.prefs.parent.mkdir()
        for path in (self.prefs, self.history, self.queue):
            path.write_bytes(b'corrupt record')
        rows = {item.key: item for item in self.checks(ffmpeg=str(self.root / 'missing-ffmpeg.exe'))}
        self.assertEqual(rows['ffmpeg'].state, 'error')
        for key, path in [('preferences', self.prefs), ('history', self.history), ('queue', self.queue)]:
            self.assertEqual(rows[key].state, 'warning')
            self.assertEqual(path.read_bytes(), b'corrupt record')

    def test_optional_components_and_runtime_are_warnings_not_critical_failures(self):
        from importlib.metadata import PackageNotFoundError, version
        def installed(name):
            if name in ('curl-cffi', 'yt-dlp-ejs'):
                raise PackageNotFoundError(name)
            return version(name)
        optional = Check('runtime', '脚本支持', 'warning', '未安装', '部分网站需要')
        with patch('video_demo.health.importlib.metadata.version', side_effect=installed), patch('video_demo.health.runtime_check', return_value=optional):
            rows = {item.key: item for item in self.checks()}
        self.assertEqual(rows['curl-cffi'].state, 'warning')
        self.assertEqual(rows['yt-dlp-ejs'].state, 'warning')
        self.assertEqual(rows['runtime'].state, 'warning')
        self.assertEqual(rows['ffmpeg'].state, 'ok')

    def test_missing_folder_is_not_created_and_sqlite_check_does_not_migrate_schema(self):
        self.folder = self.root / 'new destination'
        self.prefs.parent.mkdir()
        with closing(sqlite3.connect(self.history)) as db:
            db.execute('CREATE TABLE downloads(id INTEGER PRIMARY KEY, title TEXT)')
            db.commit()
        before = self.history.read_bytes()
        rows = {item.key: item for item in self.checks()}
        self.assertEqual(rows['folder'].state, 'info')
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.history.read_bytes(), before)
        with closing(sqlite3.connect(self.history)) as db:
            db.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(sqlite3.OperationalError):
                history_count(self.history)

    def test_output_inventory_is_bounded_nonrecursive_and_cannot_clear_mp4(self):
        for index in range(6):
            (self.folder / f'{index}.mp4').write_bytes(b'video fixture')
        nested = self.folder / 'nested'
        nested.mkdir()
        (nested / 'not-counted.mp4').write_bytes(b'nested')
        count, size, limited = output_summary(self.folder, limit=3)
        self.assertTrue(limited)
        self.assertLessEqual(count, 3)
        self.assertEqual(size, count * len(b'video fixture'))
        with self.assertRaises(ValueError):
            clear_record_file(self.folder / '0.mp4')
        self.assertTrue((self.folder / '0.mp4').exists())


class SystemUITests(WindowTestCase):
    def setUp(self):
        super().setUp()
        self.window.folder.setText(str(self.root))
        self.root_patch = patch('video_demo.system_dialog.ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            if dialog.job:
                dialog.job.wait(15000)
                self.app.processEvents()
            dialog.reject()
            dialog.deleteLater()
        if self.window.health_worker:
            self.window.health_worker.wait(15000)
            self.app.processEvents()
        super().tearDown()

    def dialog(self):
        dialog = SystemDialog(self.window, tab=1)
        self.dialogs.append(dialog)
        self.wait(lambda: dialog.job is None)
        return dialog

    def select(self, dialog, key):
        row = next(index for index, item in enumerate(dialog.rows) if item.key == key)
        dialog.data_table.selectRow(row)
        return dialog.rows[row]

    def seed(self):
        self.video = self.root / 'finished.mp4'
        self.video.write_bytes(b'completed video fixture')
        self.window.history.record(sample(), 720, self.video)
        self.window.url.setText('https://example.invalid/watch')
        self.window.populate([sample()])
        self.window.set_queue_memory(True)
        preferences.save_folder(self.root)
        self.export = self.root / 'exported.txt'
        self.export.write_text('private_cookie_fixture', encoding='utf-8')
        self.window.cookies.setText(str(self.export))
        self.progress = progress_fixture(self.root)
        log = self.root / '.local/startup.log'
        log.parent.mkdir(exist_ok=True)
        log.write_text('old startup issue', encoding='utf-8')
        self.window.log.appendPlainText('running fixture')

    def confirm(self, yes=True):
        def answer(box):
            self.assertEqual(box.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(box.defaultButton(), box.button(QMessageBox.StandardButton.No))
            self.assertIn('不删除', box.text())
            return QMessageBox.StandardButton.Yes if yes else QMessageBox.StandardButton.No
        return patch.object(QMessageBox, 'exec', new=answer)

    def test_inventory_shows_nine_categories_and_cancel_preserves_everything(self):
        self.seed()
        dialog = self.dialog()
        self.assertEqual(len(dialog.rows), 9)
        before = {path: path.read_bytes() for path in (self.video, self.export, preferences.PREFERENCES, self.window.queue_store.path, self.window.history.path, self.root / '.local/startup.log')}
        self.assertNotIn('private_cookie_fixture', repr(dialog.rows))
        with self.confirm(False):
            for key in ('preferences', 'queue', 'history', 'session', 'log', 'startup_log'):
                self.select(dialog, key)
                dialog.clear_selected()
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.window.cookies.text(), str(self.export))
        self.select(dialog, 'outputs')
        self.assertFalse(dialog.clear.isEnabled())
        self.assertTrue(self.progress.exists())

    def test_clear_history_then_queue_keeps_current_list_videos_and_partial_material(self):
        self.seed()
        dialog = self.dialog()
        with self.confirm():
            self.select(dialog, 'history')
            dialog.clear_selected()
            self.wait(lambda: dialog.job is None)
            self.select(dialog, 'queue')
            dialog.clear_selected()
            self.wait(lambda: dialog.job is None)
        self.assertEqual(self.window.history.recent(), [])
        self.assertFalse(self.window.queue_store.enabled)
        self.assertFalse(self.window.queue_store.path.exists())
        self.assertEqual(len(self.window.videos), 1)
        self.assertTrue(self.video.exists() and self.progress.exists() and self.export.exists())

    def test_forget_location_session_logs_and_startup_log_are_separate_actions(self):
        self.seed()
        dialog = self.dialog()
        with self.confirm():
            for key in ('preferences', 'session', 'startup_log', 'log'):
                self.select(dialog, key)
                dialog.clear_selected()
                self.wait(lambda: dialog.job is None)
        self.assertFalse(preferences.PREFERENCES.exists())
        self.assertEqual(self.window.folder.text(), str(self.root))
        self.assertEqual(self.window.cookies.text(), '')
        self.assertTrue(self.export.exists())
        self.assertEqual(self.export.read_text(encoding='utf-8'), 'private_cookie_fixture')
        self.assertFalse((self.root / '.local/startup.log').exists())
        self.assertEqual(self.window.log.toPlainText(), '')
        self.assertTrue(self.video.exists() and self.window.queue_store.path.exists())

    def test_partial_management_routes_to_existing_selection_and_does_not_delete(self):
        self.seed()
        dialog = self.dialog()
        self.select(dialog, 'partials')
        dialog.clear_selected()
        self.assertEqual(dialog.route, 'partials')
        self.assertTrue(self.progress.exists() and self.video.exists())

    def test_failed_record_clear_preserves_original_and_no_silent_reset(self):
        self.seed()
        dialog = self.dialog()
        self.select(dialog, 'preferences')
        before = preferences.PREFERENCES.read_bytes()
        with self.confirm(), patch('video_demo.system_dialog.clear_record_file', side_effect=PermissionError('locked')):
            dialog.clear_selected()
            self.wait(lambda: dialog.job is None)
        self.assertEqual(preferences.PREFERENCES.read_bytes(), before)
        self.assertIn('未完成', dialog.status.text())

    def test_background_startup_check_responsive_single_job_and_stale_folder_result(self):
        ticks = []
        timer = QTimer(self.window)
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        def slow_check(*_):
            time.sleep(0.2)
            return [Check('folder', '保存位置', 'ok', '可写入', 'original', 'folder')]
        with patch('video_demo.gui.environment_checks', side_effect=slow_check) as checked:
            self.window.begin_health_check()
            worker = self.window.health_worker
            self.window.begin_health_check()
            self.assertIs(self.window.health_worker, worker)
            self.window.folder.setText(str(self.root / 'changed'))
            self.wait(lambda: self.window.health_worker is None)
            self.assertEqual(checked.call_count, 1)
        timer.stop()
        self.assertGreater(len(ticks), 5)
        self.assertIn('位置已更改', self.window.health_results[0].summary)

    def test_critical_ffmpeg_blocks_operations_until_successful_recheck(self):
        self.window.url.setText('https://example.invalid/watch')
        self.window.populate([sample()])
        self.window.accept_health_results([Check('ffmpeg', '视频处理', 'error', '无法运行', 'repair')])
        with patch('video_demo.gui.QMessageBox.information'), patch.object(self.window, 'start_worker') as started:
            self.window.start_analysis()
            self.window.start_download()
            self.window.begin_batch_analysis(['https://example.invalid/watch'], None)
            self.assertFalse(self.window.begin_queue_restore())
            started.assert_not_called()
        self.assertFalse(self.window.download_button.isEnabled())
        self.assertEqual(self.window.phase_badge.text(), '视频组件需处理')
        self.window.accept_health_results([Check('ffmpeg', '视频处理', 'ok', '可用', 'ready')])
        self.assertTrue(self.window.download_button.isEnabled())
        self.assertNotEqual(self.window.phase_badge.text(), '视频组件需处理')

    def test_closing_data_dialog_waits_for_inventory_and_copies_only_on_click(self):
        original = inventory
        def slow(*args, **kwargs):
            time.sleep(0.15)
            return original(*args, **kwargs)
        with patch('video_demo.system_dialog.inventory', side_effect=slow):
            dialog = SystemDialog(self.window, tab=1)
            self.dialogs.append(dialog)
            dialog.reject()
            self.assertIsNotNone(dialog.job)
            self.wait(lambda: dialog.job is None)
        self.window.health_results = [Check('python', 'Python', 'ok', '3.12', r'C:\Users\private_name\app')]
        QApplication.clipboard().setText('synthetic previous clipboard')
        dialog = SystemDialog(self.window)
        self.dialogs.append(dialog)
        self.assertEqual(QApplication.clipboard().text(), 'synthetic previous clipboard')
        dialog.copy_checks()
        text = QApplication.clipboard().text()
        self.assertIn('Python', text)
        self.assertNotIn('private_name', text)

    def test_owner_close_during_data_scan_waits_for_thread(self):
        original = inventory
        def slow(*args, **kwargs):
            time.sleep(0.2)
            return original(*args, **kwargs)
        self.window.show()
        def close_owner():
            if self.window.system_dialog:
                self.window.close()
        with patch('video_demo.system_dialog.inventory', side_effect=slow):
            QTimer.singleShot(10, close_owner)
            self.window.show_system(tab=1)
        self.assertTrue(self.window.close_pending)
        self.assertIsNone(self.window.system_dialog)
        self.assertFalse(self.window.isVisible())


if __name__ == '__main__':
    unittest.main()
