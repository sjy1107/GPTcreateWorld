"""Real local media/Qt workflows plus simulated disk failures; no external sites."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
import errno
import http.server
import json
import shutil
import subprocess
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from tests.integration_demo import FIXTURES, make_fixtures, inspect
from tests.test_resume import RangeHandler
from tests.test_stability import sample
from tests.test_workflow_additions import WindowTestCase
from video_demo.core import Network, Video, analyze, download, convert_mp4
from video_demo.diagnostics import suggested_action
from video_demo.errors import explain_error
from video_demo.gui import Worker
from video_demo.history import HistoryStore, inspect_relocation
from video_demo.history_dialog import HistoryDialog
from video_demo.progress_dialog import ProgressDialog
from video_demo.resume import CACHE_NAME, list_progress, VERSION
from video_demo.storage import FAT32_LIMIT, MIN_FREE, StorageError, StorageGuard, VolumeInfo, preflight_destination


def progress_fixture(folder, key='a', title='保留的任务', size=4096, updated=1750000000):
    root = folder / CACHE_NAME / (key * 64)
    root.mkdir(parents=True)
    (root / 'source.mp4.part').write_bytes(b'x' * size)
    (root / 'progress.json').write_text(json.dumps({'version': VERSION, 'key': root.name,
        'title': title, 'quality': 720, 'updated': updated, 'files': {}, 'validators': {}}), encoding='utf-8')
    return root


class StoragePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_write_flush_rename_probe_and_failed_rename_cleanup(self):
        folder = self.root / 'new folder'
        preflight_destination(folder)
        self.assertTrue(folder.is_dir())
        self.assertEqual(list(folder.iterdir()), [])
        with patch('video_demo.storage.Path.rename', side_effect=PermissionError('read only')):
            with self.assertRaises(PermissionError):
                preflight_destination(folder)
        self.assertEqual(list(folder.iterdir()), [])

    def test_fat32_estimated_limit_without_creating_giant_files(self):
        with patch('video_demo.storage.volume_info', return_value=VolumeInfo('FAT32', FAT32_LIMIT)):
            with self.assertRaises(StorageError) as error:
                preflight_destination(self.root, estimated_bytes=FAT32_LIMIT + 1)
            self.assertEqual(error.exception.code, 'file_too_large')
            preflight_destination(self.root, estimated_bytes=1000)
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows path safety')
    def test_long_destination_rejected_before_directory_creation(self):
        folder = self.root / ('a' * 90) / ('b' * 90)
        with self.assertRaises(StorageError) as error:
            preflight_destination(folder)
        self.assertEqual(error.exception.code, 'path_too_long')
        self.assertFalse(folder.exists())

    def test_throttled_checks_stop_for_low_space_and_processing_has_extra_reserve(self):
        guard = StorageGuard(self.root, lambda *_: None, VolumeInfo())
        with patch('video_demo.storage.time.monotonic', side_effect=[10, 11, 12.1]), \
                patch('video_demo.storage.shutil.disk_usage', side_effect=[SimpleNamespace(free=MIN_FREE * 4), SimpleNamespace(free=MIN_FREE - 1)]) as usage:
            guard.check()
            guard.check()
            with self.assertRaises(StorageError) as error:
                guard.check()
            self.assertEqual(error.exception.code, 'disk_low')
            self.assertEqual(usage.call_count, 2)
        source = self.root / 'source.mp4'
        source.write_bytes(b'x' * 4096)
        with patch('video_demo.storage.shutil.disk_usage', return_value=SimpleNamespace(free=MIN_FREE + 4095)):
            with self.assertRaises(StorageError):
                guard.before_processing([source])

    def test_disconnected_and_replaced_folder_is_not_recreated(self):
        folder = self.root / 'drive'
        folder.mkdir()
        guard = StorageGuard(folder, lambda *_: None, VolumeInfo())
        folder.rename(self.root / 'removed')
        with self.assertRaises(StorageError) as error:
            guard.check(force=True)
        self.assertEqual(error.exception.code, 'destination_unavailable')
        self.assertFalse(folder.exists())
        folder.mkdir()
        with self.assertRaises(StorageError):
            guard.check(force=True)

    def test_runtime_file_limit_and_storage_actions_are_specific(self):
        guard = StorageGuard(self.root, lambda *_: None, VolumeInfo('FAT32', FAT32_LIMIT))
        with self.assertRaises(StorageError) as error:
            guard.check(force=True, file_bytes=FAT32_LIMIT)
        hint = explain_error(error.exception)
        self.assertEqual(hint.code, 'file_too_large')
        self.assertEqual(suggested_action(hint, 'download')[0], 'folder')

    def test_os_disk_errors_have_storage_guidance(self):
        cases = [(OSError(errno.EFBIG, 'File too large'), 'file_too_large'),
                 (OSError(errno.ENODEV, 'No such device'), 'destination_unavailable'),
                 (OSError(errno.EROFS, 'Read-only file system'), 'save_permission')]
        for error, expected in cases:
            with self.subTest(error=error):
                hint = explain_error(error)
                self.assertEqual(hint.code, expected)
                self.assertEqual(suggested_action(hint, 'download')[0], 'folder')


class StorageTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_fixtures()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), RangeHandler)
        self.server.payload = (FIXTURES / 'first.mp4').read_bytes()
        self.server.etag, self.server.supports_range = '"storage-test"', True
        self.server.change_during_resume = False
        self.server.requests, self.server.assets, self.server.tags = [], {}, {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/asset.mp4'
        self.video = analyze(self.url, threading.Event(), lambda *_: None)[0]
        self.server.requests.clear()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def test_mid_transfer_low_space_stops_queue_preserves_partial_and_resumes(self):
        info = copy.deepcopy(self.video.info)
        info.update(id='other', url=self.url.replace('asset', 'other'))
        for fmt in info.get('formats', []):
            fmt['url'] = info['url']
        second = Video.from_info(info, info['url'])
        worker = Worker('download', ([(0, self.video, None), (1, second, None)], self.root), Network(keep_progress=True))
        low, events = [False], []
        original = worker.notify
        def notify(kind, data):
            if kind == 'progress' and data.get('phase') == '下载' and (data.get('percent') or 0) > 20:
                low[0] = True
            original(kind, data)
        worker.notify = notify
        worker.event.connect(lambda kind, data: events.append((kind, data)))
        actual_usage = shutil.disk_usage
        with patch.object(StorageGuard, 'CHECK_INTERVAL', 0), \
                patch('video_demo.storage.shutil.disk_usage', side_effect=lambda path: SimpleNamespace(free=MIN_FREE - 1) if low[0] else actual_usage(path)):
            worker.run()
        self.assertTrue(worker.result['storage_stopped'])
        self.assertEqual(worker.result['pending'], 1)
        self.assertTrue(any(kind == 'row' and data['row'] == 1 and data['state'] == '已取消' for kind, data in events))
        self.assertTrue(any(kind == 'row' and data.get('hint') and data['hint'].code == 'disk_low' for kind, data in events))
        self.assertFalse(any(item['path'].startswith('/other') for item in self.server.requests))
        self.assertFalse(list(self.root.glob('*.mp4')))
        self.assertEqual(len(list_progress(self.root)), 1)
        self.server.requests.clear()
        path = download(self.video, None, self.root, threading.Event(), lambda *_: None, Network(keep_progress=True))
        inspect(path)
        self.assertTrue(any(item['if_range'] == self.server.etag and item['range'] not in ('', 'bytes=0-0') for item in self.server.requests))
        self.assertEqual(list_progress(self.root), [])

    def test_processing_space_failure_keeps_complete_source_and_retry_uses_no_network(self):
        actual_usage = shutil.disk_usage
        def usage(path):
            for manifest in self.root.glob(CACHE_NAME + '/*/progress.json'):
                data = json.loads(manifest.read_text(encoding='utf-8'))
                if data.get('ready'):
                    size = (manifest.parent / data['ready']).stat().st_size
                    return SimpleNamespace(free=MIN_FREE + size - 1)
            return actual_usage(path)
        with patch('video_demo.storage.shutil.disk_usage', side_effect=usage):
            with self.assertRaises(StorageError):
                download(self.video, None, self.root, threading.Event(), lambda *_: None, Network(keep_progress=True))
        self.assertEqual(list_progress(self.root)[0]['state'], '等待整理 MP4')
        self.assertFalse(list(self.root.glob('*.mp4')))
        self.server.requests.clear()
        path = download(self.video, None, self.root, threading.Event(), lambda *_: None, Network(keep_progress=True))
        self.assertEqual(self.server.requests, [])
        inspect(path)

    def test_low_space_during_conversion_terminates_ffmpeg(self):
        low, processes = [False], []
        original_popen = subprocess.Popen
        actual_usage = shutil.disk_usage
        def popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        def notify(kind, data):
            if kind == 'progress' and data.get('phase') in ('封装为 MP4', '转码为 MP4'):
                low[0] = True
        guard = StorageGuard(self.root, lambda *_: None)
        with patch.object(StorageGuard, 'CHECK_INTERVAL', 0), \
                patch('video_demo.core.subprocess.Popen', side_effect=popen), \
                patch('video_demo.storage.shutil.disk_usage', side_effect=lambda path: SimpleNamespace(free=0) if low[0] else actual_usage(path)):
            with self.assertRaises(StorageError):
                convert_mp4(FIXTURES / 'first.mp4', self.root / 'partial.mp4', threading.Event(), notify, storage=guard)
        self.assertGreaterEqual(len(processes), 2)  # Probe and actual media processing.
        self.assertTrue(all(process.poll() is not None for process in processes))


class RelocationStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_fixtures()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = HistoryStore(self.root / 'history.sqlite3')
        self.source = self.root / 'saved.mp4'
        shutil.copyfile(FIXTURES / 'first.mp4', self.source)
        self.store.record(sample(), 720, self.source)
        self.record = self.store.recent()[0]

    def tearDown(self):
        self.temp.cleanup()

    def test_moved_mp4_relinks_without_copy_and_remains_reusable_after_restart(self):
        target = self.root / 'renamed.mp4'
        self.source.rename(target)
        before = target.read_bytes()
        candidate = inspect_relocation(self.record, target)
        self.assertTrue(candidate.matched)
        self.store.relocate(self.record, candidate)
        reopened = HistoryStore(self.store.path)
        self.assertEqual(reopened.find(sample(), 720)['path'], str(target))
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(self.source.exists())
        inspect(target)

    def test_same_size_wrong_content_changed_candidate_and_stale_record_rejected(self):
        target = self.root / 'wrong.mp4'
        target.write_bytes(b'x' * self.record['size'])
        with self.assertRaisesRegex(ValueError, '指纹'):
            inspect_relocation(self.record, target)
        candidate = inspect_relocation(self.record, self.source)
        self.source.write_bytes(b'x' * self.record['size'])
        with self.assertRaisesRegex(ValueError, '变化'):
            self.store.relocate(self.record, candidate)
        self.assertEqual(self.store.recent()[0], self.record)
        shutil.copyfile(FIXTURES / 'first.mp4', self.source)
        candidate = inspect_relocation(self.record, self.source)
        self.store.remove(self.record['id'])
        with self.assertRaisesRegex(ValueError, '移除'):
            self.store.relocate(self.record, candidate)

    def test_old_schema_migration_and_legacy_size_only_candidate_is_explicit(self):
        legacy = HistoryStore(self.root / 'legacy.sqlite3')
        with closing(sqlite3.connect(legacy.path)) as db, db:
            db.execute('CREATE TABLE downloads (id INTEGER PRIMARY KEY, identity TEXT NOT NULL, title TEXT NOT NULL, path TEXT NOT NULL, quality INTEGER NOT NULL, size INTEGER NOT NULL, finished TEXT NOT NULL)')
            db.execute('INSERT INTO downloads VALUES(1,?,?,?,?,?,?)', ('old', '旧视频', str(self.source), 720, self.source.stat().st_size, '2026-10-19'))
        record = legacy.recent()[0]
        self.assertIsNone(record['fingerprint'])
        candidate = inspect_relocation(record, self.source)
        self.assertFalse(candidate.matched)
        legacy.relocate(record, candidate)
        self.assertTrue(legacy.recent()[0]['fingerprint'])
        wrong = self.root / 'webpage.mp4'
        wrong.write_bytes(b'x' * record['size'])
        with self.assertRaisesRegex(ValueError, '画面'):
            inspect_relocation(record, wrong)


class StorageUITests(WindowTestCase):
    def test_actual_qt_queue_stops_for_low_space_then_retry_completes_remaining_items(self):
        make_fixtures()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), RangeHandler)
        server.payload = (FIXTURES / 'first.mp4').read_bytes()
        server.etag, server.supports_range = '"qt-storage"', True
        server.change_during_resume = False
        server.requests, server.assets, server.tags = [], {}, {}
        threading.Thread(target=server.serve_forever, daemon=True).start()
        folder = self.root / 'outputs'
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            videos = [analyze(base + '/' + name, threading.Event(), lambda *_: None)[0] for name in ('one.mp4', 'two.mp4')]
            self.window.url.setText(base + '/one.mp4')
            self.window.populate(videos)
            self.window.folder.setText(str(folder))
            low = [False]
            actual_usage = shutil.disk_usage
            def usage(path):
                if any(item.stat().st_size > len(server.payload) // 5 for item in folder.glob(CACHE_NAME + '/*/*.part')):
                    low[0] = True
                return SimpleNamespace(free=MIN_FREE - 1) if low[0] else actual_usage(path)
            server.requests.clear()
            with patch.object(StorageGuard, 'CHECK_INTERVAL', 0), patch('video_demo.storage.shutil.disk_usage', side_effect=usage):
                self.window.start_download()
                self.wait(lambda: self.window.worker is None)
            self.assertEqual(self.window.states, {0: '失败', 1: '已取消'})
            self.assertIn('尚未开始', self.window.task_label.text())
            self.assertFalse(any(item['path'] == '/two.mp4' for item in server.requests))
            self.assertEqual(self.window.table.item(0, 4).data(Qt.ItemDataRole.UserRole + 1).code, 'disk_low')
            self.window.start_download(retry_only=True)
            self.wait(lambda: self.window.worker is None)
            self.assertEqual(self.window.states, {0: '已完成', 1: '已完成'})
            self.assertEqual(len(self.window.history.recent()), 2)
            for path in self.window.paths.values():
                inspect(Path(path))
            self.assertFalse(list_progress(folder))
        finally:
            if self.window.worker:
                self.window.worker.cancel.set()
                self.window.worker.wait(15000)
                self.app.processEvents()
            server.shutdown()
            server.server_close()

    def test_cache_usage_age_selection_confirmation_and_protected_unknown_records(self):
        first = progress_fixture(self.root, 'a', '第一项 <b>标题</b>', 4096, 1750000000)
        second = progress_fixture(self.root, 'b', '保留第二项', 2048, 1760000000)
        unknown = self.root / CACHE_NAME / ('c' * 64)
        unknown.mkdir()
        (unknown / 'source.mp4.part').write_bytes(b'unknown')
        final = self.root / 'finished.mp4'
        final.write_bytes(b'completed file must survive')
        dialog = ProgressDialog(self.root, self.window)
        self.assertEqual(dialog.table.rowCount(), 3)
        row = next(index for index, record in enumerate(dialog.records) if record['title'].startswith('第一项'))
        self.assertNotEqual(dialog.table.item(row, 4).text(), '时间未知')
        dialog.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.assertIn('失去', dialog.selection_hint.text())
        messages = []
        def answer(box, yes=False):
            messages.append(box.text())
            self.assertEqual(box.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(box.defaultButton(), box.button(QMessageBox.StandardButton.No))
            return QMessageBox.StandardButton.Yes if yes else QMessageBox.StandardButton.No
        with patch.object(QMessageBox, 'exec', new=answer):
            dialog.remove_selected()
        self.assertTrue(first.exists())
        with patch.object(QMessageBox, 'exec', new=lambda box: answer(box, True)):
            dialog.remove_selected()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists() and unknown.exists() and final.exists())
        self.assertIn('第一项', messages[0])
        self.assertIn('重新下载', messages[0])
        unknown_row = next(i for i, record in enumerate(dialog.records) if not record.get('can_clear', True))
        self.assertFalse(dialog.table.item(unknown_row, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable)
        dialog.deleteLater()

    def test_background_storage_summary_and_invalid_destination(self):
        progress_fixture(self.root)
        self.window.folder.setText(str(self.root))
        self.window.refresh_storage_info()
        self.assertFalse(self.window.progress_files_button.isEnabled())
        self.wait(lambda: self.window.storage_worker is None)
        self.assertTrue(self.window.progress_files_button.isEnabled())
        self.assertIn('未完成素材占用', self.window.storage_hint.text())
        self.assertIn('可用', self.window.storage_hint.text())
        self.window.folder.setText(str(self.root / 'not-created'))
        self.window.refresh_storage_info()
        self.assertIn('尚不存在', self.window.storage_hint.text())

    def test_gui_relink_cancel_then_confirm_updates_only_selected_record(self):
        make_fixtures()
        path = self.root / 'old.mp4'
        shutil.copyfile(FIXTURES / 'first.mp4', path)
        self.window.history.record(sample(), 720, path)
        target = self.root / 'moved.mp4'
        path.rename(target)
        self.window.url.setText('https://example.invalid/watch')
        self.window.populate([sample()])
        self.window.handle_event('row', {'row': 0, 'state': '已完成', 'path': str(path), 'existing': True})
        dialog = HistoryDialog(self.window.history, self.window)
        dialog.relocated.connect(self.window.update_history_path)
        dialog.table.selectRow(0)
        before = self.window.history.recent()[0]
        for answer in [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]:
            with patch('video_demo.history_dialog.QFileDialog.getOpenFileName', return_value=(str(target), '')), \
                    patch.object(QMessageBox, 'exec', return_value=answer):
                dialog.relink_file()
                self.wait(lambda: dialog.relink_worker is None)
            record = self.window.history.recent()[0]
            if answer == QMessageBox.StandardButton.No:
                self.assertEqual(record, before)
                self.assertEqual(self.window.paths[0], str(path))
            else:
                self.assertEqual(record['path'], str(target))
                self.assertEqual(self.window.paths[0], str(target))
                self.assertEqual(record['id'], before['id'])
                self.assertEqual(record['finished'], before['finished'])
        self.assertTrue(target.exists())
        self.assertFalse(path.exists())
        self.assertEqual(len(self.window.history.recent()), 1)
        dialog.deleteLater()

    def test_unwritable_destination_is_explained_before_worker_starts(self):
        self.window.url.setText('https://example.invalid/watch')
        self.window.populate([sample()])
        self.window.folder.setText(str(self.root))
        with patch('video_demo.gui.preflight_destination', side_effect=PermissionError('Permission denied')), \
                patch('video_demo.gui.QMessageBox.warning') as warning, patch.object(self.window, 'start_worker') as start:
            self.window.start_download()
        start.assert_not_called()
        self.assertIn('写入', warning.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
