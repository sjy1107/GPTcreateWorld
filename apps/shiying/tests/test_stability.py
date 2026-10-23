"""Regressions for state preservation, bounded preflight work and bad media."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

from tests.integration_demo import FIXTURES, inspect, make_fixtures
from tests.test_resume import RangeHandler
from video_demo import transfer
from video_demo.browser_cookies import BrowserCookieError, BrowserSession, load_cookie_text, site_scope
from video_demo.core import Network, Video, download
from video_demo.errors import InvalidMediaError, explain_error, redact_detail
from video_demo.gui import MainWindow, Worker, duration_label, speed_label
from video_demo.history import HistoryStore
from video_demo.help_ui import HelpDialog
from video_demo.usage_guide import UsageGuide
from video_demo.resume import list_progress


def sample(index=0):
    return Video.from_info({"id": str(index), "title": "Sample", "extractor_key": "Fixture", "duration": 3,
        "formats": [{"format_id": "720", "url": f"https://example.invalid/{index}.mp4", "protocol": "https",
                     "ext": "mp4", "height": 720, "vcodec": "h264", "acodec": "aac", "filesize": 10000 + index}]}, "https://example.invalid/watch")


class StabilityTests(unittest.TestCase):
    def test_windows_and_clipboard_line_endings_never_enter_cookie_values(self):
        line = ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic_only"
        for newline in ("\n", "\r\n", "\r"):
            jar = load_cookie_text(newline.join(["# Netscape HTTP Cookie File", line, ""]))
            self.assertEqual(next(iter(jar)).value, "synthetic_only")
        with self.assertRaises(ValueError) as error:
            load_cookie_text("# Netscape HTTP Cookie File\n" + line + "\x00private_value\n")
        self.assertNotIn("private_value", str(error.exception))
        self.assertNotIn("private_value", redact_detail("Invalid header value b'SESSDATA=private_value'"))

    def test_metadata_without_valid_numbers_does_not_crash_the_ui(self):
        item = sample()
        item.info["duration"] = "not provided"
        item.info["formats"].extend([{"height": float("inf"), "vcodec": "h264"}, {"height": "720", "vcodec": "h264"}])
        parsed = Video.from_info(item.info, item.referer)
        self.assertIsNone(parsed.duration)
        self.assertEqual(parsed.heights, [720])
        for value in (None, float("inf"), float("nan"), -1, "unknown"):
            self.assertEqual(duration_label(value), "时长未知")
            self.assertEqual(speed_label(value), "速度计算中")
        with self.assertRaises(ValueError) as caught:
            Video.from_info({"formats": [{"vcodec": "none", "acodec": "aac"}]}, "https://example.invalid/audio")
        self.assertNotEqual(explain_error(caught.exception).code, "protected")

    def test_explicit_membership_message_is_explained_but_url_slugs_are_not(self):
        hint = explain_error(RuntimeError("HTTP Error 403: This video is members-only"))
        self.assertEqual(hint.code, "membership_required")
        self.assertIn("权限", hint.as_text())
        plain = explain_error(RuntimeError("HTTP Error 403: https://example.invalid/members-only"))
        self.assertEqual(plain.code, "access_denied")

    def test_space_estimation_initializes_once_for_a_real_mixed_queue(self):
        videos = [sample(index) for index in range(50)]
        original = transfer.yt_dlp.YoutubeDL
        with patch.object(transfer.yt_dlp, "YoutubeDL", wraps=original) as constructor, patch.object(original, "urlopen", side_effect=AssertionError("Offline estimate requested network")):
            result = transfer.estimate_space([(index, item, None) for index, item in enumerate(videos)])
        self.assertEqual(constructor.call_count, 1)
        self.assertEqual(result.source_bytes, sum(10000 + index for index in range(50)))
        self.assertEqual(result.unknown, 0)

    def test_duplicate_check_uses_one_connection_and_rejects_missing_or_wrong_quality(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            store = HistoryStore(folder / "history.sqlite3")
            videos = [sample(index) for index in range(5)]
            saved = folder / "saved.mp4"
            saved.write_bytes(b"metadata-only history fixture")
            for item in videos:
                store.record(item, 720, saved)
            with patch.object(store, "connect", wraps=store.connect) as connect:
                found = store.find_many([(i, videos[i % 5], 720 if i != 9 else 1080) for i in range(50)])
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(len(found), 49)
            self.assertNotIn(9, found)
            saved.unlink()
            self.assertEqual(store.find_many([(0, videos[0], 720)]), {})

    def test_html_instead_of_video_is_not_kept_and_next_retry_fetches_media(self):
        make_fixtures()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        server.payload = b"<html><body>Access page, not a video</body></html>" * 100
        server.etag = '"html-page"'
        server.assets, server.tags, server.requests = {}, {}, []
        server.supports_range, server.change_during_resume = True, False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder)
                item = sample()
                item.info["formats"][0]["url"] = f"http://127.0.0.1:{server.server_port}/asset.mp4"
                item.info["formats"][0]["protocol"] = "http"
                with patch.dict(os.environ, {"NO_PROXY": "localhost,127.0.0.1,::1"}):
                    with self.assertRaises(InvalidMediaError):
                        download(item, None, target, threading.Event(), lambda *_: None, Network(keep_progress=True))
                    self.assertEqual(list_progress(target), [])
                    self.assertFalse(list(target.glob("*.mp4")))
                    server.payload = (FIXTURES / "first.mp4").read_bytes()
                    server.etag = '"actual-media"'
                    server.requests.clear()
                    result = download(item, None, target, threading.Event(), lambda *_: None, Network(keep_progress=True))
                inspect(result)
                self.assertTrue(server.requests)
                self.assertEqual(server.requests[0]["range"], "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class StableGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.patches = [patch("video_demo.gui.discover_profiles", return_value=[]),
                        patch("video_demo.preferences.PREFERENCES", self.folder / "preferences.json")]
        for item in self.patches:
            item.start()
        self.window = MainWindow(history_path=self.folder / "history.sqlite3")
        self.window.url.setText("https://www.bilibili.com/video/synthetic")
        self.window.populate([sample(0), sample(1)])

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_failed_or_cancelled_browser_read_preserves_working_session(self):
        jar = load_cookie_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic_only\n")
        session = BrowserSession("clipboard", site_scope(self.window.url.text()), tuple(jar))
        for cancelled in (False, True):
            self.window.browser_session = session
            self.window.browser_profile.setText("synthetic-profile")
            self.window.update_cookie_status()
            self.assertIn("已添加", self.window.source_hint.text())
            with patch.object(self.window, "start_worker"):
                self.window.start_cookie_read()
            self.assertIs(self.window.browser_session, session)
            worker = Worker("cookies", ("edge", "synthetic-profile", self.window.url.text()))
            if cancelled:
                worker.cancelled = True
            else:
                worker.fail(BrowserCookieError("locked", "Synthetic database busy"))
            self.window.worker = worker
            with patch.object(self.window, "error_dialog"):
                self.window.worker_finished()
            self.assertIs(self.window.browser_session, session)
            self.assertIn("仍保留", self.window.progress_label.text())

    def test_startup_failure_marks_queued_rows_retryable_and_preserves_successes(self):
        self.window.states[0] = "排队中"
        self.window.states[1] = "已完成"
        self.window.paths[1] = "previous.mp4"
        worker = Worker("download", ([], self.folder))
        worker.fail(PermissionError("Temporary directory is not writable"))
        self.window.worker = worker
        with patch.object(self.window, "error_dialog"):
            self.window.worker_finished()
        self.assertEqual(self.window.states[0], "失败")
        self.assertEqual(self.window.states[1], "已完成")
        self.assertTrue(self.window.retry_button.isEnabled())
        self.assertTrue(self.window.table.item(0, 4).toolTip())

    def test_failed_redownload_does_not_open_an_old_copy_and_missing_file_is_explained(self):
        path = self.folder / "previous.mp4"
        path.write_bytes(b"metadata fixture")
        self.window.paths[0] = str(path)
        self.window.states[0] = "失败"
        with patch("video_demo.gui.QDesktopServices.openUrl", return_value=True) as opened:
            self.window.open_video(0, 1)
            opened.assert_not_called()
            self.window.states[0] = "已完成"
            self.window.open_video(0, 1)
            self.assertEqual(opened.call_count, 1)
            path.unlink()
            with patch("video_demo.gui.QMessageBox.information") as message:
                self.window.open_video(0, 1)
            self.assertEqual(opened.call_count, 1)
            self.assertIn("找不到", message.call_args.args[1])

    def test_unchanged_cookie_file_is_not_reparsed_on_each_action(self):
        path = self.folder / "cookies.txt"
        path.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic_only\n")
        self.window.cookies.setText(str(path))
        from video_demo.gui import load_cookie_file
        with patch("video_demo.gui.load_cookie_file", wraps=load_cookie_file) as load:
            self.window.network_settings()
            self.window.network_settings()
            self.assertEqual(load.call_count, 1)
            path.write_text('{"invalid":"changed-file-must-be-rechecked"}')
            with self.assertRaises(ValueError):
                self.window.network_settings()
            self.assertEqual(load.call_count, 2)

    def test_repeated_help_windows_are_released_after_closing(self):
        def close_help():
            dialog = self.app.activeModalWidget()
            if isinstance(dialog, (HelpDialog, UsageGuide)):
                dialog.accept()
        for _ in range(5):
            QTimer.singleShot(0, close_help)
            self.window.show_quick_help()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(self.window.findChildren(HelpDialog), [])
        self.assertEqual(self.window.findChildren(UsageGuide), [])


if __name__ == "__main__":
    unittest.main()
