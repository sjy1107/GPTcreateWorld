import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import functools
import http.server
import tempfile
import threading
import time
import unittest
import yt_dlp
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox, QPlainTextEdit

from tests.integration_demo import FIXTURES, Handler, inspect, make_fixtures
from video_demo.browser_cookies import load_cookie_file
from video_demo.core import MAX_VIDEOS, Network, Video, analyze, download
from video_demo.gui import MainWindow, Worker
from video_demo.transfer import MAX_LINKS, SPACE_MARGIN, estimate_space, estimate_video_bytes, parse_urls


def video(identity="one", referer="https://www.bilibili.com/video/one"):
    return Video(identity, identity, 10, [1080, 720], {"id": identity, "extractor_key": "Fixture", "formats": [
        {"format_id": "audio", "url": "https://example.com/audio", "ext": "m4a", "vcodec": "none", "acodec": "aac", "filesize": 100},
        {"format_id": "720", "url": f"https://example.com/{identity}.720", "ext": "mp4", "vcodec": "h264", "acodec": "none", "height": 720, "filesize": 1000},
        {"format_id": "1080", "url": f"https://example.com/{identity}.1080", "ext": "mp4", "vcodec": "h264", "acodec": "none", "height": 1080, "filesize": 2000},
    ]}, referer)


class TransferTests(unittest.TestCase):
    def test_batch_input_validates_every_line_and_deduplicates(self):
        self.assertEqual(parse_urls("\n https://example.com/a#fragment\nhttps://example.com/a\nhttps://example.com/b?a=1&b=2\n"),
                         ["https://example.com/a", "https://example.com/b?a=1&b=2"])
        for text in ("", "https://example.com/ok\nnot-a-link", "file:///C:/private", "https://example.com/a https://example.com/b",
                     "\n".join(f"https://example.com/{n}" for n in range(MAX_LINKS + 1))):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_urls(text)

    def test_estimate_respects_quality_and_includes_separate_audio_without_network(self):
        item = video()
        item.info["formats"][-1]["__needs_testing"] = True
        with patch.object(yt_dlp.YoutubeDL, "urlopen", side_effect=AssertionError("Offline estimate made a network request")):
            self.assertEqual(estimate_video_bytes(item, 720), 1100)
            self.assertEqual(estimate_video_bytes(item, None), 2100)
        estimate = estimate_space([(0, item, 720), (1, item, None)])
        self.assertEqual(estimate.source_bytes, 3200)
        self.assertEqual(estimate.recommended_bytes, 3200 + 4200 + SPACE_MARGIN)
        self.assertEqual(estimate.unknown, 0)

    def test_bitrate_estimate_and_unknown_track_are_never_zero_size(self):
        item = video()
        audio = item.info["formats"][0]
        audio.pop("filesize")
        audio["tbr"] = 8
        self.assertEqual(estimate_video_bytes(item, 720), 11000)
        audio.pop("tbr")
        self.assertIsNone(estimate_video_bytes(item, 720))
        estimate = estimate_space([(0, item, 720)])
        self.assertEqual(estimate.unknown, 1)

    def test_mixed_site_cookie_files_are_scoped_for_each_lookup_and_each_download(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cookies.txt"
            path.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tbili_synthetic\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tins_synthetic\n")
            original = path.read_bytes()
            urls = ["https://www.bilibili.com/video/one", "https://www.instagram.com/reel/two", "https://example.org/public"]
            videos = [video(str(index), url) for index, url in enumerate(urls)]
            copies = []
            def verify_network(url, network):
                self.assertEqual(network.rate_limit, 12345)
                if "example.org" in url:
                    self.assertEqual(network.cookie_file, "")
                else:
                    copy_path = Path(network.cookie_file)
                    copies.append(copy_path)
                    jar = load_cookie_file(copy_path)
                    self.assertEqual(len(jar), 1)
                    self.assertEqual(next(iter(jar)).domain, ".bilibili.com" if "bilibili" in url else ".instagram.com")
            def lookup(url, cancel, notify, network):
                verify_network(url, network)
                return [videos[urls.index(url)]]
            with patch("video_demo.gui.analyze", side_effect=lookup):
                worker = Worker("analyze_batch", urls, Network(cookie_file=str(path), rate_limit=12345))
                worker.run()
            self.assertFalse(worker.error, worker.error)
            self.assertEqual(len(worker.result["videos"]), 3)
            self.assertTrue(copies)
            self.assertTrue(all(not p.exists() for p in copies))
            copies.clear()
            def save(item, height, target, cancel, notify, network):
                verify_network(item.referer, network)
                return Path(folder) / (item.key + ".mp4")
            with patch("video_demo.gui.download", side_effect=save):
                worker = Worker("download", ([(i, v, None) for i, v in enumerate(videos)], Path(folder)), Network(cookie_file=str(path), rate_limit=12345))
                worker.run()
            self.assertFalse(worker.error, worker.error)
            self.assertEqual(worker.result["ok"], 3)
            self.assertTrue(all(not p.exists() for p in copies))
            self.assertEqual(path.read_bytes(), original)

    def test_one_failed_link_does_not_stop_batch_and_duplicates_are_removed(self):
        urls = ["https://example.com/one", "https://example.com/broken", "https://example.com/alias", "https://example.com/two"]
        def lookup(url, *args):
            if "broken" in url:
                raise TimeoutError("timed out")
            return [video("two" if "two" in url else "one")]
        worker = Worker("analyze_batch", urls)
        with patch("video_demo.gui.analyze", side_effect=lookup):
            worker.run()
        self.assertFalse(worker.error)
        self.assertEqual([v.title for v in worker.result["videos"]], ["one", "two"])
        self.assertEqual(worker.result["failures"][0]["url"], urls[1])
        self.assertEqual(worker.result["processed"], 4)

    def test_batch_cap_keeps_remaining_urls_and_cancellation_stops_requests(self):
        urls = ["https://example.com/many", "https://example.com/next"]
        worker = Worker("analyze_batch", urls)
        with patch("video_demo.gui.analyze", return_value=[video(str(i)) for i in range(MAX_VIDEOS)]) as lookup:
            worker.run()
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(len(worker.result["videos"]), MAX_VIDEOS)
        self.assertEqual(worker.result["pending"], urls[1:])
        cancelled = Worker("analyze_batch", urls)
        def stop(*args):
            cancelled.cancel.set()
            return [video()]
        with patch("video_demo.gui.analyze", side_effect=stop) as lookup:
            cancelled.run()
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(lookup.call_count, 1)

    def test_actual_http_batch_and_rate_limit_create_decodable_mp4(self):
        make_fixtures()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"NO_PROXY": "localhost,127.0.0.1,::1"}):
                worker = Worker("analyze_batch", [base + "/first.mp4", base + "/missing.mp4", base + "/linked.mp4"])
                worker.run()
                self.assertFalse(worker.error, worker.error)
                self.assertEqual(len(worker.result["videos"]), 2)
                self.assertEqual(len(worker.result["failures"]), 1)
                started = time.monotonic()
                path = download(worker.result["videos"][0], None, Path(folder), threading.Event(), lambda *_: None,
                                Network(rate_limit=256 * 1024))
                elapsed = time.monotonic() - started
                # About 650 KB requires >2 seconds at 256 KB/s. This proves the
                # limiter affects real transfer, rather than just changing a label.
                self.assertGreater(elapsed, 1.8)
                self.assertLess(elapsed, 30)
                inspect(path)
                self.assertFalse(list(Path(folder).glob(".video-work-*")))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class TransferGuiTests(unittest.TestCase):
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
        self.window = MainWindow(history_path=self.root / "history.sqlite3")
        self.window.url.setText("https://www.bilibili.com/video/one")
        self.window.populate([video()])
        self.window.folder.setText(str(self.root))

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_batch_dialog_submits_lines_and_multiline_paste_opens_it(self):
        labels = []
        def enter():
            dialog = self.app.activeModalWidget()
            if isinstance(dialog, QDialog):
                editor = dialog.findChild(QPlainTextEdit)
                editor.setPlainText("https://example.com/one\nhttps://example.com/two")
                button = next(b for b in dialog.findChild(QDialogButtonBox).buttons() if b.text() == "查找这些链接")
                labels.append(button.text())
                button.click()
        with patch.object(self.window, "start_worker") as start:
            QTimer.singleShot(0, enter)
            self.window.batch_button.click()
        self.assertEqual(start.call_args.args[0], "analyze_batch")
        self.assertEqual(len(start.call_args.args[1]), 2)
        self.assertEqual(labels, ["查找这些链接"])
        QApplication.clipboard().setText("https://example.com/a\nhttps://example.com/b")
        with patch.object(self.window, "show_batch_input") as batch:
            self.window.paste_button.click()
        self.assertIn("\n", batch.call_args.args[0])

    def test_failed_batch_preserves_previous_results_and_exposes_retry_links(self):
        old = list(self.window.videos)
        self.window.paths[0] = "existing.mp4"
        worker = Worker("analyze_batch", ["https://example.com/broken"])
        worker.result = {"videos": [], "failures": [{"index": 1, "url": worker.payload[0], "hint": "请求超时"}], "processed": 1, "pending": []}
        self.window.worker = worker
        self.window.worker_finished()
        self.assertEqual(self.window.videos, old)
        self.assertEqual(self.window.paths[0], "existing.mp4")
        self.assertEqual(self.window.failed_urls, worker.payload)
        self.assertFalse(self.window.lookup_report_button.isHidden())

    def test_low_space_returns_without_network_unless_user_explicitly_continues(self):
        captured = []
        def answer(proceed=False):
            dialog = self.app.activeModalWidget()
            if isinstance(dialog, QMessageBox):
                captured.append(dialog.text())
                next(b for b in dialog.buttons() if b.text() == ("仍然继续" if proceed else "返回调整")).click()
        with patch("video_demo.gui.shutil.disk_usage", return_value=SimpleNamespace(free=1024)), patch.object(self.window, "start_worker") as start:
            QTimer.singleShot(0, answer)
            self.window.start_download()
            start.assert_not_called()
            QTimer.singleShot(0, lambda: answer(True))
            self.window.start_download()
            self.assertEqual(start.call_count, 1)
        self.assertEqual(len(captured), 2)
        self.assertIn("建议至少预留", captured[0])

    def test_rate_limit_is_frozen_during_download_and_progress_keeps_live_speed(self):
        self.window.rate_limit.setCurrentIndex(2)
        self.assertEqual(self.window.network_settings().rate_limit, 1024**2)
        worker = Worker("download", ([], self.root))
        self.window.worker = worker
        self.window.update_actions()
        self.assertFalse(self.window.rate_limit.isEnabled())
        self.window.handle_event("progress", {"phase": "下载", "percent": 40, "speed": 500 * 1024, "eta": 10})
        self.assertIn("500.0 KB/s", self.window.progress_label.text())
        self.window.worker = None
        self.window.update_actions()
        worker.deleteLater()
        self.assertTrue(self.window.rate_limit.isEnabled())


if __name__ == "__main__":
    unittest.main()
