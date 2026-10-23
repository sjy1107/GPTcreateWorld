import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
import yt_dlp
from tests.test_transfer import video
from video_demo.gui import MainWindow, Worker


class SizeHintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = patch("video_demo.preferences.PREFERENCES", self.root / "preferences.json")
        self.settings.start()
        self.window = MainWindow(history_path=self.root / "history.sqlite3")
        self.window.url.setText("https://www.bilibili.com/video/one")
        self.window.populate([video("one"), video("two")])
        self.window.size_timer.stop()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.settings.stop()
        self.temp.cleanup()

    def test_menu_and_selected_total_include_audio_without_network(self):
        with patch.object(yt_dlp.YoutubeDL, "urlopen", side_effect=AssertionError("Size hints must be offline")):
            self.window.refresh_size_hints()
            self.window.prepare_quality_menu(0)
            combo = self.window.table.cellWidget(0, 3)
            self.assertEqual(combo.currentData(), None)
            self.assertIn("2.1 KB", combo.itemText(combo.findData(None)))
            self.assertIn("1.1 KB", combo.itemText(combo.findData(720)))
            self.assertIn("4.1 KB", self.window.progress_label.text())
            self.window.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
            combo.setCurrentIndex(combo.findData(720))
            self.window.refresh_size_hints()
            self.assertIn("1.1 KB", self.window.progress_label.text())
            self.assertEqual(combo.currentData(), 720)

    def test_unknown_size_is_visible_and_not_reported_as_zero(self):
        item = self.window.videos[0]
        for fmt in item.info["formats"]:
            fmt.pop("filesize", None)
        self.window.refresh_size_hints()
        self.assertIn("大小未知", self.window.table.cellWidget(0, 3).currentText())
        self.assertIn("1 项大小未知", self.window.progress_label.text())
        self.assertNotIn("0.0 B", self.window.progress_label.text())

    def test_cached_choices_are_reused_and_fresh_metadata_invalidates_cache(self):
        original = yt_dlp.YoutubeDL
        with patch("video_demo.transfer.yt_dlp.YoutubeDL", wraps=original) as downloader:
            self.window.refresh_size_hints()
            self.window.prepare_quality_menu(0)
            count = downloader.call_count
            self.window.refresh_size_hints()
            self.window.prepare_quality_menu(0)
            self.assertEqual(downloader.call_count, count)
        item = self.window.videos[0]
        item.info = copy.deepcopy(item.info)
        item.info["formats"][-1]["filesize"] = 8000
        self.window.refresh_size_hints()
        self.assertIn("7.9 KB", self.window.table.cellWidget(0, 3).currentText())

    def test_idle_estimates_do_not_overwrite_active_transfer_progress(self):
        worker = unittest.mock.Mock()
        worker.cancel.is_set.return_value = False
        self.window.worker = worker
        self.window.progress_label.setText("下载 42% · 2 MB/s")
        self.window.refresh_size_hints()
        self.assertEqual(self.window.progress_label.text(), "下载 42% · 2 MB/s")
        self.window.worker = None

    def test_pending_estimate_after_row_completion_does_not_overwrite_saved_hint(self):
        worker = Worker("download", ([], self.root))
        worker.result = {"ok": 2, "failed": 0, "skipped": 0}
        self.window.worker = worker
        for row in range(2):
            path = self.root / f"fixture-{row}.mp4"
            path.write_bytes(b"synthetic history metadata fixture")
            self.window.handle_event("row", {"row": row, "state": "已完成", "path": str(path)})
        self.assertTrue(self.window.size_timer.isActive())
        self.window.worker_finished()
        expected = self.window.progress_label.text()
        self.assertIn("文件已保存", expected)
        QTest.qWait(200)
        self.assertEqual(self.window.progress_label.text(), expected)


if __name__ == "__main__":
    unittest.main()
