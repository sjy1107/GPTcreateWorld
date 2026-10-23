import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from video_demo.core import Video
from video_demo.gui import MainWindow
from video_demo.history import HistoryStore, video_identity
from video_demo.history_dialog import HistoryDialog


def video(identifier='one', url='https://www.bilibili.com/video/one?token=synthetic_secret'):
    return Video(identifier, 'Sample video', 3, [1080, 720], {'extractor_key': 'BiliBili', 'id': identifier}, url)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = HistoryStore(self.root / 'history.sqlite3')
        self.path = self.root / 'saved.mp4'
        self.path.write_bytes(b'synthetic metadata-only fixture')

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_identity_quality_and_no_credentials_stored(self):
        self.store.record(video(), 720, self.path)
        reopened = HistoryStore(self.store.path)
        same = video(url='https://www.bilibili.com/video/one?different_share_parameter=yes')
        self.assertEqual(reopened.find(same, 720)['path'], str(self.path))
        self.assertIsNone(reopened.find(same, 1080))
        self.assertIsNone(reopened.find(video('other'), 720))
        serialized = json.dumps(reopened.recent())
        self.assertNotIn('synthetic_secret', serialized)
        self.assertNotIn('bilibili.com', serialized)
        self.assertNotIn('synthetic_secret', self.store.path.read_bytes().decode('latin1'))

    def test_deleted_or_changed_file_is_not_reused(self):
        self.store.record(video(), None, self.path)
        self.path.write_bytes(b'truncated')
        self.assertIsNone(self.store.find(video(), None))
        self.path.unlink()
        self.assertIsNone(self.store.find(video(), None))

    def test_generic_urls_do_not_merge_different_tokens_ids_or_ports(self):
        def generic(url):
            return Video('random', 'same title', 3, [], {'formats': [{'url': url, 'vcodec': 'h264'}]}, 'https://example.com/')
        for other in ('https://example.com/get.mp4?token=B', 'https://example.com/get.mp4?id=B', 'https://example.com:8080/get.mp4?token=A'):
            self.assertNotEqual(video_identity(generic('https://example.com/get.mp4?token=A')), video_identity(generic(other)))

    def test_record_removal_never_deletes_video(self):
        self.store.record(video(), 720, self.path)
        record = self.store.recent()[0]
        self.store.remove(record['id'])
        self.assertEqual(self.store.recent(), [])
        self.assertTrue(self.path.is_file())
        self.store.record(video(), None, self.path)
        self.store.remove()
        self.assertTrue(self.path.is_file())

    def test_history_stays_bounded(self):
        self.store.record(video(), None, self.path)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.executemany('INSERT INTO downloads(identity,title,path,quality,size,finished) VALUES(?,?,?,?,?,?)',
                           [('fixture', 'Old', str(self.path), 0, self.path.stat().st_size, '2026-10-18T00:00:00+00:00')] * 500)
        self.store.record(video('new'), None, self.path)
        self.assertEqual(len(self.store.recent()), 500)
        self.assertEqual(self.store.recent()[0]['identity'], video_identity(video('new')))


class HistoryGUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pref = patch('video_demo.preferences.PREFERENCES', self.root / 'preferences.json')
        self.profiles = patch('video_demo.gui.discover_profiles', return_value=[])
        self.pref.start()
        self.profiles.start()
        self.window = MainWindow(history_path=self.root / 'history.sqlite3')
        self.window.url.setText('https://www.bilibili.com/video/one')
        self.window.populate([video()])
        self.window.folder.setText(str(self.root))
        self.path = self.root / 'saved.mp4'
        self.path.write_bytes(b'synthetic metadata-only fixture')
        self.window.history.record(video(), None, self.path)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.profiles.stop()
        self.pref.stop()
        self.temp.cleanup()

    def answer(self, text):
        def click():
            dialog = self.app.activeModalWidget()
            if isinstance(dialog, QMessageBox):
                button = next((b for b in dialog.buttons() if b.text() == text), None)
                if button:
                    button.click()
                else:
                    dialog.reject()
        QTimer.singleShot(0, click)

    def test_reuse_existing_file_never_starts_network_or_adds_history(self):
        self.answer('使用已有文件')
        with patch.object(self.window, 'start_worker') as start:
            self.window.start_download()
        start.assert_not_called()
        self.assertEqual(self.window.paths[0], str(self.path))
        self.assertEqual(self.window.states[0], '已完成')
        self.assertEqual(len(self.window.history.recent()), 1)

    def test_explicit_redownload_and_cancel_choices(self):
        items = [(0, self.window.videos[0], None)]
        self.answer('仍然重新下载')
        self.assertEqual(self.window.check_previous_downloads(items), items)
        self.answer('取消')
        self.assertEqual(self.window.check_previous_downloads(items), [])
        self.assertEqual(self.window.states[0], '待下载')

    def test_history_search_open_and_missing_file_guidance(self):
        dialog = HistoryDialog(self.window.history, self.window)
        try:
            dialog.search.setText('not present')
            self.assertEqual(dialog.table.rowCount(), 0)
            dialog.search.clear()
            self.assertEqual(dialog.table.rowCount(), 1)
            dialog.table.selectRow(0)
            with patch('video_demo.history_dialog.QDesktopServices.openUrl') as open_file:
                dialog.open_file()
            self.assertEqual(open_file.call_args.args[0].toLocalFile(), str(self.path).replace('\\', '/'))
            self.path.unlink()
            with patch('video_demo.history_dialog.QMessageBox.information') as info:
                dialog.open_file()
            self.assertTrue(info.called)
            dialog.remove_selected()
            self.assertEqual(self.window.history.recent(), [])
        finally:
            dialog.deleteLater()

    def test_broken_history_does_not_block_download(self):
        self.window.history.path.write_bytes(b'broken database')
        with patch.object(self.window, 'start_worker') as start:
            self.window.start_download()
        self.assertEqual(start.call_args.args[0], 'download')
        self.assertIn('继续正常下载', self.window.log.toPlainText())


if __name__ == '__main__':
    unittest.main()
