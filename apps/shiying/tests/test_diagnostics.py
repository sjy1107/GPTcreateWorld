import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import functools
import http.server
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

from video_demo.core import analyze
from video_demo.diagnostics import DiagnosticDialog, build_report
from video_demo.errors import AnalysisError, explain_error
from video_demo.gui import MainWindow, Worker
from tests.test_stability import sample
from tests.integration_demo import FIXTURES, inspect, make_fixtures


class LookupFailureTests(unittest.TestCase):
    def failed_lookup(self, primary, extra):
        with patch("video_demo.core.yt_dlp.YoutubeDL.extract_info", side_effect=primary), \
                patch("video_demo.core.page_candidates", side_effect=extra):
            with self.assertRaises(AnalysisError) as caught:
                analyze("https://example.com/watch", threading.Event(), lambda *_: None)
        return explain_error(caught.exception, "analyze")

    def test_fallback_does_not_change_server_failure_into_login_failure(self):
        hint = self.failed_lookup(RuntimeError("HTTP Error 500: Domain Not Found"), RuntimeError("HTTP Error 401"))
        self.assertEqual(hint.status, 500)
        self.assertNotEqual(hint.code, "authentication")
        self.assertIn("401", hint.detail)

    def test_fresh_cookies_not_reported_as_rate_limited_by_secondary_scan(self):
        hint = self.failed_lookup(RuntimeError("Fresh cookies (not necessarily logged in) are needed"), RuntimeError("HTTP Error 429"))
        self.assertEqual(hint.code, "session_required")
        self.assertIsNone(hint.status)
        self.assertIn("429", hint.detail)

    def test_site_access_requirements_survive_secondary_forbidden_response(self):
        cases = [
            ("ERROR: [Ixigua] sample: Cookies (not necessarily logged in) are needed", "session_required"),
            ("ERROR: [Reddit] sample: Account authentication is required. Use --cookies to provide credentials", "authentication"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                hint = self.failed_lookup(RuntimeError(message), RuntimeError("HTTP Error 403: Forbidden"))
                self.assertEqual(hint.code, expected)
                self.assertIsNone(hint.status)
                self.assertIn("403", hint.detail)
        self.assertIn("不一定要求账号登录", explain_error(RuntimeError(cases[0][0])).reason)

    def test_unsupported_main_extractor_uses_actionable_scan_error(self):
        hint = self.failed_lookup(RuntimeError("Unsupported URL: https://example.com/watch"), TimeoutError("timed out"))
        self.assertEqual(hint.code, "timeout")

    def test_url_text_cannot_invent_http_login_or_drm_evidence(self):
        for slug in ("HTTP%20Error%20403", "login_required", "drm", "fresh-cookies"):
            hint = self.failed_lookup(RuntimeError(f"Unsupported URL: https://example.com/{slug}"), RuntimeError("Unsupported URL"))
            self.assertEqual(hint.code, "not_detected")
            self.assertIsNone(hint.status)

    def test_vimeo_login_wording_is_recognized(self):
        self.assertEqual(explain_error(RuntimeError("The web client only works when logged-in. Use --cookies to provide credentials")).code,
                         "authentication")


class DiagnosticUITests(unittest.TestCase):
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
        self.window.url.setText("https://example.com/watch")
        self.window.populate([sample(0), sample(1)])

    def tearDown(self):
        QApplication.clipboard().clear()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_report_redacts_urls_credentials_and_local_paths(self):
        hint = explain_error(RuntimeError("HTTP Error 403\nHTTPS://user:password@example.com/private?token=URLSECRET\n"
                                         "{'Cookie': 'COOKIESECRET'}\nauth_token=TOKENVALUE\nproxy.local:7890 refused\n"
                                         "C:\\Users\\secret_name\\Downloads\\cookies.txt"))
        report = build_report(hint, "download", "https://user:password@example.com/private?key=hidden", login="file", proxy="http://proxy.local:7890")
        for secret in ("password", "URLSECRET", "COOKIESECRET", "TOKENVALUE", "secret_name", "private?", "key=hidden", "proxy.local"):
            self.assertNotIn(secret, report)
        self.assertIn("网站：example.com", report)
        self.assertIn("access_denied", report)
        self.assertIn("yt-dlp", report)

    def test_copy_requires_click_and_does_not_close_dialog(self):
        hint = explain_error(TimeoutError("timed out"))
        QApplication.clipboard().setText("previous clipboard")
        dialog = DiagnosticDialog("失败", hint.as_text(), hint, "synthetic report", "analyze", self.window)
        dialog.show()
        self.assertEqual(QApplication.clipboard().text(), "previous clipboard")
        dialog.copy_button.click()
        self.assertEqual(QApplication.clipboard().text(), "synthetic report")
        self.assertTrue(dialog.isVisible())
        dialog.detail_toggle.click()
        self.assertFalse(dialog.details.isHidden())
        dialog.close()
        dialog.deleteLater()

    def test_rate_limited_and_protected_errors_do_not_encourage_immediate_retry(self):
        for error in (RuntimeError("HTTP Error 429"), RuntimeError("DRM protected")):
            hint = explain_error(error)
            dialog = DiagnosticDialog("失败", hint.as_text(), hint, "report", "analyze", self.window)
            self.assertNotIn("retry", dialog.action_buttons)
            dialog.deleteLater()

    def test_browser_busy_action_opens_file_import_without_reading_again(self):
        with patch.object(self.window, "start_cookie_read") as read:
            self.window.run_error_action("import", {"operation": "cookies"})
            self.assertTrue(self.window.network_panel.isVisible())
            self.assertEqual(self.window.login_tabs.currentIndex(), 1)
            read.assert_not_called()
        self.window.network_toggle.setChecked(False)

    def test_retry_targets_only_selected_failed_row(self):
        make_fixtures()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.server.requests.append(self.path)
                if self.server.deny and self.path.split("?", 1)[0] in ("/first.mp4", "/second.webm"):
                    self.send_error(404)
                else:
                    super().do_GET()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES)))
        server.deny, server.requests = False, []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def wait_for(predicate):
            end = time.monotonic() + 15
            while time.monotonic() < end:
                self.app.processEvents()
                if predicate():
                    return
                time.sleep(0.005)
            self.fail("Qt worker did not finish before deadline")
        try:
            url = f"http://127.0.0.1:{server.server_port}/index.html"
            videos = analyze(url, threading.Event(), lambda *_: None)[:2]
            self.window.url.setText(url)
            self.window.populate(videos)
            self.window.folder.setText(str(self.root / "outputs"))
            server.deny = True
            self.window.start_download()
            wait_for(lambda: self.window.worker is None)
            self.assertEqual(self.window.states, {0: "失败", 1: "失败"})
            server.deny = False
            server.requests.clear()
            self.window.table.selectRow(1)
            def retry():
                dialog = QApplication.activeModalWidget()
                self.assertIsInstance(dialog, DiagnosticDialog)
                dialog.action_buttons["retry"].click()
            QTimer.singleShot(10, retry)
            self.window.show_failure()
            wait_for(lambda: self.window.states[1] == "已完成" and self.window.worker is None)
            self.assertEqual(self.window.states[0], "失败")
            self.assertNotIn("/first.mp4", server.requests)
            self.assertIn("/second.webm", server.requests)
            self.assertEqual(len(self.window.history.recent()), 1)
            inspect(Path(self.window.paths[1]))
        finally:
            if self.window.worker:
                self.window.worker.cancel.set()
                self.window.worker.wait(15000)
                self.app.processEvents()
            server.shutdown()
            server.server_close()

    def test_deferred_retry_starts_after_old_worker_cleanup(self):
        worker = Worker("analyze", self.window.url.text())
        worker.fail(TimeoutError("timed out"))
        self.window.worker = worker
        def click_retry():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, DiagnosticDialog)
            self.assertIsNone(self.window.worker)
            dialog.action_buttons["retry"].click()
        QTimer.singleShot(10, click_retry)
        with patch.object(self.window, "start_analysis") as retry:
            self.window.worker_finished()
            retry.assert_not_called()
            self.app.processEvents()
            retry.assert_called_once()


if __name__ == "__main__":
    unittest.main()
