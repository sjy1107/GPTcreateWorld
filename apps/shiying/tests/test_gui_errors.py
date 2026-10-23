import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent

from video_demo.core import Video
from video_demo.errors import explain_error
from video_demo.gui import MainWindow
from video_demo.gui import Worker
from video_demo.core import Network
from video_demo.browser_cookies import BrowserSession


class GUIErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        cls.app.processEvents()

    def test_selected_failed_video_shows_its_own_suggestions(self):
        window = MainWindow()
        try:
            window.populate([Video("one", "First", 3, [720], {}, ""), Video("two", "Second", 3, [720], {}, "")])
            for row, error in enumerate((TimeoutError("timed out"), RuntimeError("HTTP Error 403: Forbidden"))):
                hint = explain_error(error)
                window.handle_event("row", {"row": row, "state": "失败", "error": hint.as_text(), "detail": hint.detail})
            window.update_actions()
            self.assertTrue(window.error_button.isEnabled())
            window.table.selectRow(1)
            with patch.object(window, "error_dialog") as dialog:
                window.error_button.click()
            title, message, detail = dialog.call_args.args
            self.assertIn("Second", message)
            self.assertIn("资源服务器拒绝访问", message)
            self.assertIn("建议", message)
            self.assertIn("403", detail)
        finally:
            window.close()
            window.deleteLater()

    def test_browser_import_is_explicit_and_other_site_does_not_get_session(self):
        window = MainWindow()
        try:
            window.url.setText("https://instagram.com/reel/demo/")
            window.cookie_browser.setCurrentIndex(window.cookie_browser.findData('firefox'))
            window.browser_profile.setText("test-profile")
            window.network_toggle.setChecked(True)
            with patch.object(window, "start_worker") as start:
                window.read_cookies.click()
            operation, payload, _ = start.call_args.args
            self.assertEqual(operation, "cookies")
            self.assertEqual(payload, ("firefox", "test-profile", "https://instagram.com/reel/demo/"))
            session = BrowserSession("firefox", ("instagram.com",), ())
            worker = Worker("cookies", payload)
            worker.result = session
            window.worker = worker
            window.worker_finished()
            self.assertIs(window.browser_session, session)
            with patch("video_demo.gui.Worker.start"):
                window.start_worker("analyze", window.url.text(), Network())
                self.assertIs(window.worker.session, session)
                window.worker.deleteLater()
                window.worker = None
                window.url.setText("https://x.com/example/status/1")
                window.start_worker("analyze", window.url.text(), Network())
                self.assertIsNone(window.worker.session)
                window.worker.deleteLater()
                window.worker = None
            window.update_actions()
            window.clear_cookies.click()
            self.assertIsNone(window.browser_session)
        finally:
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
