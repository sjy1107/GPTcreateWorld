import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from video_demo.core import Video
from video_demo.errors import explain_error
from video_demo.gui import MainWindow, Worker


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = patch("video_demo.preferences.PREFERENCES", Path(self.temp.name) / "preferences.json")
        self.settings.start()
        self.window = MainWindow(history_path=Path(self.temp.name) / 'history.sqlite3')
        self.window.url.setText("https://example.com/video")
        self.window.populate([Video("one", "One", 3, [1080, 720, 360], {}, ""),
                              Video("two", "Two", 3, [2160], {}, "")])

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.settings.stop()
        self.temp.cleanup()

    def test_selection_and_changed_link_prevent_wrong_download(self):
        window = self.window
        window.select_all.setChecked(False)
        self.assertFalse(window.download_button.isEnabled())
        window.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        self.assertTrue(window.download_button.isEnabled())
        self.assertIn("1", window.download_button.text())
        window.url.setText("https://example.com/other")
        self.assertFalse(window.download_button.isEnabled())
        with patch.object(window, "start_worker") as start, patch("video_demo.gui.QMessageBox.information"):
            window.start_download()
        start.assert_not_called()
        window.url.setText("https://example.com/video")
        self.assertTrue(window.download_button.isEnabled())

    def test_batch_quality_respects_selection_and_never_chooses_higher(self):
        window = self.window
        window.batch_quality.setCurrentIndex(window.batch_quality.findData(720))
        self.assertEqual(window.table.cellWidget(0, 3).currentData(), 720)
        self.assertIsNone(window.table.cellWidget(1, 3).currentData())
        window.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        window.batch_quality.setCurrentIndex(window.batch_quality.findData(None))
        self.assertEqual(window.table.cellWidget(0, 3).currentData(), 720)

    def test_failed_lookup_preserves_previous_videos_and_saved_paths(self):
        window = self.window
        window.paths[0] = "already-saved.mp4"
        old = list(window.videos)
        window.url.setText("https://example.com/broken")
        worker = Worker("analyze", window.url.text())
        worker.error_hint = explain_error(TimeoutError("timed out"), "analyze")
        worker.error = worker.error_hint.as_text()
        window.worker = worker
        with patch.object(window, "error_dialog"):
            window.worker_finished()
        self.assertEqual(window.videos, old)
        self.assertEqual(window.paths[0], "already-saved.mp4")
        self.assertFalse(window.download_button.isEnabled())

    def test_preference_save_failure_does_not_prevent_download(self):
        window = self.window
        window.folder.setText(self.temp.name)
        with patch("video_demo.gui.save_folder", side_effect=PermissionError("read-only")), patch.object(window, "start_worker") as start:
            window.start_download()
        self.assertEqual(start.call_args.args[0], "download")
        self.assertIn("不影响本次下载", window.log.toPlainText())


if __name__ == "__main__":
    unittest.main()
