import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import functools
import http.server
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from tests.integration_demo import FIXTURES, Handler, inspect, make_fixtures
from tests.test_transfer import video
from video_demo.core import analyze
from video_demo.gui import MainWindow
from video_demo.history import video_identity
from video_demo.queue_dialog import QueueDialog
from video_demo.queue_store import QueueStore, restore_queue


def job(item, quality=None):
    return {"url": item.referer, "identity": video_identity(item), "title": item.title, "quality": quality}


class QueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = QueueStore(self.root / "queue.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_opt_in_and_field_whitelist(self):
        item = job(video(), 720)
        item.update(cookie="secret", formats=[{"url": "https://cdn.test/temporary?token=secret"}])
        self.store.save([item], self.root)
        self.assertFalse(self.store.path.exists())
        self.store.enabled = True
        self.store.save([item], self.root)
        saved = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("secret", saved)
        self.assertNotIn("formats", saved)
        loaded = QueueStore(self.store.path)
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.snapshot["items"], [job(video(), 720)])

    def test_atomic_failure_preserves_previous_snapshot(self):
        self.store.enabled = True
        self.store.save([job(video("first"))], self.root)
        before = self.store.path.read_bytes()
        with patch("video_demo.queue_store.os.replace", side_effect=PermissionError("read-only")):
            with self.assertRaises(OSError):
                self.store.save([job(video("second"))], self.root)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".queue-*.tmp")))

    def test_invalid_or_oversized_records_are_not_restored(self):
        for data in (b"{broken", b"0" * (1024 * 1024 + 1),
                     json.dumps({"version": 1, "enabled": True, "folder": "x", "items": [{"url": None}]}).encode()):
            self.store.path.write_bytes(data)
            loaded = QueueStore(self.store.path)
            self.assertTrue(loaded.load_error)
            self.assertFalse(loaded.enabled)
            self.assertFalse(loaded.snapshot["items"])

    def test_restore_groups_pages_and_keeps_unmatched_or_changed_quality(self):
        first, missing, quality_changed = video("first"), video("missing"), video("changed")
        for item in (first, missing, quality_changed):
            item.referer = first.referer
        snapshot = {"folder": str(self.root), "items": [job(first, 720), job(missing), job(quality_changed, 1080)]}
        quality_changed.heights = [720]
        lookup = unittest.mock.Mock(return_value=[first, quality_changed])
        result = restore_queue(snapshot, lookup, threading.Event(), lambda *_: None)
        lookup.assert_called_once_with(first.referer)
        self.assertEqual(result["videos"], [first])
        self.assertEqual(result["qualities"], [720])
        self.assertEqual(len(result["pending"]), 2)
        self.assertTrue(any("1080p" in issue for issue in result["issues"]))


class QueueUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch("video_demo.gui.discover_profiles", return_value=[]),
                        patch("video_demo.preferences.PREFERENCES", self.root / "preferences.json")]
        for item in self.patches:
            item.start()
        self.windows = []
        self.window = self.new_window()
        self.window.url.setText("https://www.bilibili.com/video/one")
        self.window.populate([video("one"), video("two")])
        self.window.folder.setText(str(self.root / "outputs"))

    def new_window(self):
        window = MainWindow(history_path=self.root / "history.sqlite3")
        self.windows.append(window)
        return window

    def tearDown(self):
        for window in self.windows:
            if window.worker:
                window.worker.cancel.set()
                window.worker.wait(15000)
                self.app.processEvents()
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def wait(self, predicate):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Queue worker did not finish")

    def test_default_does_not_persist_urls_and_explicit_opt_in_can_clear(self):
        self.window.persist_queue()
        self.assertFalse(self.window.queue_store.path.exists())
        dialog = QueueDialog(self.window)
        self.assertFalse(dialog.remember.isChecked())
        dialog.remember.click()
        self.assertTrue(self.window.queue_store.path.is_file())
        self.assertEqual(dialog.table.rowCount(), 2)
        output = self.root / "already_completed.mp4"
        output.write_bytes(b"synthetic final file")
        dialog.clear.click()
        self.assertFalse(self.window.queue_store.path.exists())
        self.assertFalse(self.window.queue_store.enabled)
        self.assertTrue(output.exists())
        dialog.deleteLater()

    def test_selection_quality_changes_and_success_update_saved_items(self):
        self.assertTrue(self.window.set_queue_memory(True))
        self.window.table.cellWidget(0, 3).setCurrentIndex(self.window.table.cellWidget(0, 3).findData(720))
        self.window.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        self.window.persist_queue()
        self.assertEqual(len(self.window.queue_store.snapshot["items"]), 1)
        self.assertEqual(self.window.queue_store.snapshot["items"][0]["quality"], 720)
        self.window.states[0] = "已完成"
        self.window.persist_queue()
        self.assertEqual(self.window.queue_store.snapshot["items"], [])

    def test_all_failed_restore_preserves_queue_and_write_failure_is_nonfatal(self):
        self.window.set_queue_memory(True)
        before = self.window.queue_store.path.read_bytes()
        fresh = self.new_window()
        with patch("video_demo.gui.analyze", side_effect=TimeoutError("timed out")):
            self.assertTrue(fresh.begin_queue_restore())
            self.wait(lambda: fresh.worker is None)
        self.assertFalse(fresh.videos)
        self.assertTrue(fresh.queue_issues)
        self.assertEqual(fresh.queue_store.path.read_bytes(), before)
        with patch.object(fresh.queue_store, "clear", side_effect=PermissionError("read-only")):
            self.assertFalse(fresh.set_queue_memory(False))
        self.assertTrue(fresh.queue_store.enabled)

    def test_partial_restore_retains_unresolved_jobs_in_snapshot(self):
        self.window.set_queue_memory(True)
        fresh = self.new_window()
        with patch("video_demo.gui.analyze", return_value=[video("one")]):
            fresh.begin_queue_restore()
            self.wait(lambda: fresh.worker is None)
        self.assertEqual(len(fresh.videos), 1)
        self.assertEqual(len(fresh.queue_unresolved), 1)
        self.assertEqual(len(fresh.queue_issues), 1)
        self.assertEqual(len(fresh.queue_store.snapshot["items"]), 2)
        fresh.states[0] = "已完成"
        fresh.persist_queue()
        self.assertEqual(len(fresh.queue_store.snapshot["items"]), 1)

    def test_restart_reparse_restore_and_actual_mp4_download(self):
        make_fixtures()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/index.html"
            videos = analyze(url, threading.Event(), lambda *_: None)
            first = videos[0]
            hls = next(item for item in videos if 180 in item.heights and 360 in item.heights)
            self.window.url.setText(url)
            self.window.populate([first, hls])
            self.window.table.cellWidget(1, 3).setCurrentIndex(self.window.table.cellWidget(1, 3).findData(180))
            self.window.set_queue_memory(True)
            self.window.close()
            saved = self.window.queue_store.path.read_text(encoding="utf-8")
            self.assertNotIn("master.m3u8", saved)
            self.assertNotIn("formats", saved)
            fresh = self.new_window()
            self.assertFalse(fresh.videos)
            with patch("video_demo.gui.analyze", wraps=analyze) as lookup:
                self.assertTrue(fresh.begin_queue_restore())
                self.wait(lambda: fresh.worker is None)
            lookup.assert_called_once()
            self.assertEqual(len(fresh.videos), 2)
            self.assertEqual(fresh.table.cellWidget(1, 3).currentData(), 180)
            self.assertEqual(fresh.folder.text(), str(self.root / "outputs"))
            self.assertFalse((self.root / "outputs").exists())
            fresh.start_download()
            self.wait(lambda: fresh.worker is None)
            self.assertTrue(all(state == "已完成" for state in fresh.states.values()))
            for path in fresh.paths.values():
                inspect(Path(path))
            fresh.persist_queue()
            self.assertEqual(fresh.queue_store.snapshot["items"], [])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
