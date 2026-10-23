import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import functools
import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

from tests.integration_demo import FIXTURES, inspect, make_fixtures
from video_demo.browser_cookies import load_cookie_text
from video_demo.cookie_guide import CHROMIUM_EXPORTER, FIREFOX_EXPORTER, CookieGuide
from video_demo.core import Network
from video_demo.gui import MainWindow, Worker

EXPORT = ("# Netscape HTTP Cookie File\n"
          ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic_session_not_real\n"
          ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tunrelated_synthetic\n")


class CookieGuideTests(unittest.TestCase):
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
        self.window.url.setText("https://www.bilibili.com/video/synthetic")
        self.window.cookie_browser.setCurrentIndex(self.window.cookie_browser.findData("edge"))
        self.guide = CookieGuide(self.window)

    def tearDown(self):
        QApplication.clipboard().clear()
        self.guide.close()
        self.guide.deleteLater()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_guide_walks_through_edge_steps_without_reading_or_opening_anything(self):
        with patch("video_demo.cookie_guide.QDesktopServices.openUrl") as opened, patch("video_demo.gui.read_browser_session") as reader:
            self.guide.show()
            texts = []
            for index in range(4):
                texts.append(self.guide.content.toPlainText())
                self.assertEqual(self.guide.counter.text(), f"{index + 1} / 4")
                if index < 3:
                    self.guide.next.click()
            opened.assert_not_called()
            reader.assert_not_called()
        self.assertIn("确认视频能播放", texts[0])
        self.assertIn("kairi003", texts[1])
        self.assertIn("允许来自其他商店", texts[1])
        for word in ("Netscape", "Copy", "Export As", "Export All Cookies", "视频页面"):
            self.assertIn(word, texts[2])
        for word in ("Ctrl+J", "从剪贴板导入", "选择刚导出的文件", "重新", "权限"):
            self.assertIn(word, texts[3])

    def test_primary_guide_button_opens_actual_dialog(self):
        captured = []
        def close():
            dialog = self.app.activeModalWidget()
            if isinstance(dialog, CookieGuide):
                captured.append(dialog.content.toPlainText())
                dialog.accept()
        QTimer.singleShot(0, close)
        self.window.cookie_guide_button.click()
        self.assertEqual(len(captured), 1)
        self.assertIn("Edge", captured[0])

    def test_explicit_store_and_copy_buttons_use_verified_browser_specific_links(self):
        with patch("video_demo.cookie_guide.QDesktopServices.openUrl", return_value=True) as opened:
            self.guide.open_store()
            self.assertEqual(opened.call_args.args[0].toString(), CHROMIUM_EXPORTER)
            self.guide.copy_store()
            self.assertEqual(QApplication.clipboard().text(), CHROMIUM_EXPORTER)
            self.guide.browser.setCurrentIndex(self.guide.browser.findData("firefox"))
            self.guide.move(1)
            self.assertIn("Lennon Hill", self.guide.content.toPlainText())
            self.guide.open_store()
            self.assertEqual(opened.call_args.args[0].toString(), FIREFOX_EXPORTER)
        self.guide.copy_target()
        self.assertEqual(QApplication.clipboard().text(), self.window.url.text())

    def test_downloads_shortcut_handles_custom_location_and_missing_folder(self):
        with patch("video_demo.cookie_guide.downloads_folder", return_value=self.root), patch("video_demo.cookie_guide.QDesktopServices.openUrl", return_value=True) as opened:
            self.guide.show_downloads()
            self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), self.root)
        with patch("video_demo.cookie_guide.downloads_folder", return_value=self.root / "missing"), patch("video_demo.cookie_guide.QDesktopServices.openUrl") as opened:
            self.guide.show_downloads()
            opened.assert_not_called()
            self.assertIn("Ctrl+J", self.guide.message.text())

    def test_clipboard_import_is_scoped_and_invalid_input_preserves_working_login(self):
        self.window.accept_cookie_text(EXPORT)
        session = self.window.browser_session
        self.assertEqual(len(session.cookies), 1)
        self.assertTrue(session.login_marker)
        self.assertEqual(session.browser, "clipboard")
        self.assertIn("剪贴板", self.window.cookie_status.text())
        self.assertEqual(self.window.cookies.text(), "")
        for bad in ("https://example.com/video", '{"cookie":"synthetic_private_value"}',
                    "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsynthetic\n",
                    "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t1\tSESSDATA\texpired\n"):
            with self.assertRaises(ValueError):
                self.window.accept_cookie_text(bad)
            self.assertIs(self.window.browser_session, session)
        self.assertNotIn("synthetic_session_not_real", self.window.log.toPlainText())
        self.window.clear_login_information()
        self.assertIsNone(self.window.browser_session)

    def test_guide_clipboard_errors_stay_inline_and_success_is_clear(self):
        self.guide.step = 3
        self.guide.render()
        QApplication.clipboard().setText("not a cookie export")
        self.guide.import_clipboard.click()
        self.assertIsNone(self.window.browser_session)
        self.assertIn("Netscape", self.guide.message.text())
        QApplication.clipboard().setText(EXPORT)
        self.guide.import_clipboard.click()
        self.assertIsNotNone(self.window.browser_session)
        self.assertIn("已导入", self.guide.done_button.text())
        self.assertIn("重新查找", self.guide.message.text())

    def test_guide_file_import_and_same_path_reimport_refresh_status(self):
        path = self.root / "export.txt"
        path.write_text(EXPORT)
        with patch("video_demo.gui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.guide.choose_export()
            self.assertIn("1 条", self.window.cookie_status.text())
            self.assertIn("已导入", self.guide.message.text())
            path.write_text(EXPORT + ".bilibili.com\tTRUE\t/\tTRUE\t0\tsid\tsecond_synthetic\n")
            self.guide.choose_export()
            self.assertIn("2 条", self.window.cookie_status.text())
        with self.assertRaises(ValueError):
            self.window.accept_cookie_text("a bad clipboard value")
        self.assertEqual(self.window.cookies.text(), str(path))
        self.window.url.setText("https://x.com/test/status/1")
        self.assertNotIn("2 条", self.window.cookie_status.text())
        self.assertIn("公开访问", self.window.cookie_status.text())

    def test_bom_and_httponly_text_use_same_parser_as_file(self):
        jar = load_cookie_text("\ufeff" + EXPORT.replace(".bilibili.com\t", "#HttpOnly_.bilibili.com\t"), self.window.url.text())
        self.assertEqual(len(jar), 1)
        with self.assertRaises(ValueError) as error:
            load_cookie_text("# Netscape HTTP Cookie File\nprivate malformed cookie value\n")
        self.assertNotIn("private malformed", str(error.exception))

    def test_clipboard_session_can_download_protected_local_media_and_is_cleared(self):
        make_fixtures()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if "session_clipboard=synthetic" not in self.headers.get("Cookie", ""):
                    self.server.rejections += 1
                    self.send_error(401)
                    return
                return super().do_GET()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES)))
        server.rejections = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/first.mp4"
            self.window.url.setText(url)
            self.window.accept_cookie_text("# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tsession_clipboard\tsynthetic\n")
            session = self.window.browser_session
            with patch.dict(os.environ, {"NO_PROXY": "localhost,127.0.0.1,::1"}):
                lookup = Worker("analyze", url, Network(), session)
                lookup.run()
                self.assertFalse(lookup.error, lookup.error)
                save = Worker("download", ([(0, lookup.result[0], None)], self.root), Network(), session)
                save.run()
                self.assertFalse(save.error, save.error)
                self.assertEqual(save.result["ok"], 1)
            self.assertEqual(server.rejections, 0)
            outputs = list(self.root.glob("*.mp4"))
            self.assertEqual(len(outputs), 1)
            inspect(outputs[0])
            self.window.close()
            self.assertIsNone(self.window.browser_session)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
