import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import concurrent.futures
import http.server
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication
from tests.integration_demo import FIXTURES, inspect, make_fixtures
from tests.test_resume import RangeHandler
from tests.test_transfer import video
from video_demo.core import Network, analyze, download
from video_demo.gui import MainWindow, Worker
from video_demo.naming import filename_stem, publish_mp4
from video_demo.naming_dialog import NamingDialog
from video_demo.transfer import MAX_LINKS, shared_urls


class SharedInputTests(unittest.TestCase):
    def test_share_text_wrappers_and_query_parameters(self):
        url = "https://example.com/watch?token=A+B%2F%3D&sign=abc.;!"
        self.assertEqual(shared_urls(f"分享这个视频：{url}。复制后打开"), [url])
        self.assertEqual(shared_urls(f"[视频]({url})"), [url])
        self.assertEqual(shared_urls("打开（https://example.com/a_(b)），即可观看"), ["https://example.com/a_(b)"])
        self.assertEqual(shared_urls("https://example.com/video。复制打开"), ["https://example.com/video"])

    def test_bare_url_is_not_trimmed_or_rewritten(self):
        for url in ("https://example.com/a?signature=a+b%2F=&last=.)", "https://example.com/clip_(1)"):
            self.assertEqual(shared_urls(url), [url])

    def test_multiple_links_are_deduplicated_and_limited(self):
        self.assertEqual(shared_urls("说明 https://example.com/a#one 和 https://example.com/b\n再次 https://example.com/a#two"),
                         ["https://example.com/a", "https://example.com/b"])
        with self.assertRaises(ValueError):
            shared_urls(" ".join(f"https://example.com/{n}" for n in range(MAX_LINKS + 1)))

    def test_invalid_or_credential_urls_do_not_leak_or_partially_submit(self):
        for text in ("没有网址", "ftp://example.com/a", "分享 https://user:SECRET@example.com/video", "https://example.com:bad/video"):
            with self.assertRaises(ValueError) as caught:
                shared_urls(text)
            self.assertNotIn("SECRET", str(caught.exception))


class NamingTests(unittest.TestCase):
    def test_simple_rules_extensions_and_windows_names(self):
        self.assertEqual(filename_stem("旅行", 720), "旅行_720p")
        self.assertEqual(filename_stem("旅行", 720, naming="title"), "旅行")
        self.assertEqual(filename_stem("原标题", 720, filename="自定.mp4.mp4"), "自定")
        self.assertEqual(filename_stem("x", filename="CON.mp4"), "_CON")
        self.assertNotIn("/", filename_stem("x", filename="../escape.mp4"))

    def test_concurrent_publication_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "旅行.mp4").write_bytes(b"existing")
            sources = []
            for i in range(4):
                source = root / f"temp-{i}.bin"
                source.write_bytes(f"new-{i}".encode())
                sources.append(source)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                paths = list(pool.map(lambda source: publish_mp4(source, root, "旅行"), sources))
            self.assertEqual(len(set(paths)), 4)
            self.assertEqual((root / "旅行.mp4").read_bytes(), b"existing")
            self.assertEqual({path.read_bytes() for path in paths}, {f"new-{i}".encode() for i in range(4)})


class WindowTestCase(unittest.TestCase):
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

    def new_window(self):
        window = MainWindow(history_path=self.root / "history.sqlite3")
        self.windows.append(window)
        return window

    def tearDown(self):
        QApplication.clipboard().clear()
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
        end = time.monotonic() + 25
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Worker did not finish before deadline")


class AdditionUITests(WindowTestCase):
    def test_clipboard_paste_and_direct_share_input_do_not_start_unrequested_network(self):
        QApplication.clipboard().setText("文字\nhttps://example.com/video?token=a%2Fb\n复制打开")
        with patch.object(self.window, "start_worker") as start:
            self.window.paste_button.click()
            start.assert_not_called()
            self.assertEqual(self.window.url.text(), "https://example.com/video?token=a%2Fb")
            self.window.url.setText("直接粘贴 分享 https://example.com/next")
            self.window.start_analysis()
            self.assertEqual(start.call_args.args[1], "https://example.com/next")
        with patch.object(self.window, "show_batch_input") as batch, patch.object(self.window, "start_worker") as start:
            self.window.url.setText("两个 https://example.com/a 和 https://example.com/b")
            self.window.start_analysis()
            batch.assert_called_once_with("https://example.com/a\nhttps://example.com/b")
            start.assert_not_called()

    def test_naming_preview_apply_and_queue_restore_preserve_original_title(self):
        self.window.url.setText("https://www.bilibili.com/video/one")
        self.window.populate([video("one")])
        original_title = self.window.videos[0].title
        def edit():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, NamingDialog)
            dialog.table.item(0, 1).setText("我的/收藏.mp4")
            self.assertEqual(dialog.table.item(0, 2).text(), "我的_收藏.mp4")
            dialog.mode.setCurrentIndex(dialog.mode.findData("title"))
            dialog.accept()
        QTimer.singleShot(10, edit)
        self.window.show_naming()
        self.assertEqual(self.window.videos[0].title, original_title)
        self.assertEqual(self.window.naming_mode, "title")
        self.window.set_queue_memory(True)
        fresh = self.new_window()
        with patch("video_demo.gui.analyze", return_value=[video("one")]):
            fresh.begin_queue_restore()
            self.wait(lambda: fresh.worker is None)
        self.assertEqual(fresh.custom_names[0], "我的/收藏.mp4")
        self.assertEqual(fresh.naming_mode, "title")

    def test_cancelling_naming_dialog_does_not_apply_changes(self):
        self.window.url.setText("https://www.bilibili.com/video/one")
        self.window.populate([video()])
        def cancel():
            dialog = QApplication.activeModalWidget()
            dialog.table.item(0, 1).setText("should not be used")
            dialog.reject()
        QTimer.singleShot(10, cancel)
        self.window.show_naming()
        self.assertEqual(self.window.custom_names, {})


class SkipTransferTests(WindowTestCase):
    def setUp(self):
        super().setUp()
        make_fixtures()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        self.server.payload = (FIXTURES / "first.mp4").read_bytes()
        self.server.etag = '"skip-test-content"'
        self.server.supports_range = True
        self.server.change_during_resume = False
        self.server.requests = []
        self.server.assets, self.server.tags = {}, {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.urls = [f"http://127.0.0.1:{self.server.server_port}/{name}.mp4" for name in ("first", "second", "third")]

    def tearDown(self):
        super().tearDown()
        self.server.shutdown()
        self.server.server_close()

    def test_actual_gui_skip_continues_then_retry_resumes_only_skipped_item(self):
        videos = [analyze(url, threading.Event(), lambda *_: None)[0] for url in self.urls]
        self.window.url.setText(self.urls[0])
        self.window.populate(videos)
        self.window.folder.setText(str(self.root / "outputs"))
        self.window.rate_limit.setCurrentIndex(self.window.rate_limit.findData(512 * 1024))
        self.window.custom_names = {0: "首项", 1: "我的收藏.mp4", 2: "我的收藏.mp4"}
        flags = {"skipped": False, "stale": False}
        original = self.window.handle_event
        def handle(kind, data):
            original(kind, data)
            if kind == "progress" and self.window.current_row == 0 and data.get("phase") == "下载" and (data.get("percent") or 0) > 20 and not flags["skipped"]:
                self.assertTrue(self.window.skip_button.isEnabled())
                self.window.skip_button.click()
                flags["skipped"] = True
            if kind == "current" and data["row"] == 1:
                self.assertFalse(self.window.worker.request_skip(0))
                flags["stale"] = True
        self.window.handle_event = handle
        self.window.start_download()
        self.wait(lambda: self.window.worker is None)
        self.assertTrue(all(flags.values()))
        self.assertEqual(self.window.states, {0: "已跳过", 1: "已完成", 2: "已完成"})
        self.assertNotIn(0, self.window.paths)
        self.assertEqual(Path(self.window.paths[1]).name, "我的收藏.mp4")
        self.assertEqual(Path(self.window.paths[2]).name, "我的收藏 (2).mp4")
        self.assertTrue(self.window.retry_button.isEnabled())
        self.server.requests.clear()
        self.window.start_download(retry_only=True)
        self.wait(lambda: self.window.worker is None)
        self.assertTrue(all(state == "已完成" for state in self.window.states.values()))
        self.assertEqual({request["path"] for request in self.server.requests}, {"/first.mp4"})
        self.assertTrue(any(request["range"].startswith("bytes=") and request["range"] != "bytes=0-0" and request["if_range"] for request in self.server.requests))
        for path in self.window.paths.values():
            inspect(Path(path))

    def test_skipping_conversion_without_retention_continues_and_leaves_no_partial(self):
        videos = [analyze(url, threading.Event(), lambda *_: None)[0] for url in self.urls[:2]]
        worker = Worker("download", ([(i, v, None) for i, v in enumerate(videos)], self.root), Network(keep_progress=False))
        rows = []
        def event(kind, data):
            if kind == "progress" and data.get("phase") == "封装为 MP4" and worker.active_row == 0:
                worker.request_skip(0)
            if kind == "row":
                rows.append(data)
        worker.event.connect(event, Qt.ConnectionType.DirectConnection)
        worker.run()
        self.assertEqual(worker.result["skipped"], 1)
        self.assertEqual(worker.result["ok"], 1)
        self.assertTrue(any(item["row"] == 0 and item["state"] == "已跳过" for item in rows))
        self.assertEqual(len(list(self.root.glob("*.mp4"))), 1)
        self.assertFalse(list(self.root.glob(".video-work-*")))
        self.assertFalse(worker.request_skip(0))

    def test_cancel_all_takes_precedence_over_skip(self):
        videos = [analyze(url, threading.Event(), lambda *_: None)[0] for url in self.urls[:2]]
        worker = Worker("download", ([(i, v, None) for i, v in enumerate(videos)], self.root))
        rows = []
        def event(kind, data):
            if kind == "progress" and data.get("phase") == "下载":
                worker.request_skip(0)
                worker.cancel.set()
            if kind == "row":
                rows.append(data)
        worker.event.connect(event, Qt.ConnectionType.DirectConnection)
        worker.run()
        self.assertTrue(worker.cancelled)
        self.assertFalse(any(item["row"] == 1 for item in rows))
        self.assertFalse(list(self.root.glob("*.mp4")))

    def test_default_names_use_detected_height_and_custom_names_do_not_change_identity(self):
        item = analyze(self.urls[0], threading.Event(), lambda *_: None)[0]
        title, identity = item.title, item.info["id"]
        output = download(item, None, self.root, threading.Event(), lambda *_: None)
        self.assertTrue(output.name.endswith("_360p.mp4"), output.name)
        self.assertEqual((item.title, item.info["id"]), (title, identity))
        inspect(output)


if __name__ == "__main__":
    unittest.main()
