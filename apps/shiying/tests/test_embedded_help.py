import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication
from video_demo.gui import MainWindow
from video_demo.help_ui import HelpDialog
from video_demo.usage_guide import UsageGuide


class EmbeddedHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = patch('video_demo.gui.discover_profiles', return_value=[])
        self.profiles.start()
        self.window = MainWindow(history_path=self.root / 'history.sqlite3')

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.profiles.stop()
        self.temp.cleanup()

    def test_help_buttons_show_complete_steps_inside_app(self):
        self.window.cookie_browser.setCurrentIndex(self.window.cookie_browser.findData('edge'))
        captured = []
        def capture():
            dialog = self.app.activeModalWidget()
            if isinstance(dialog, UsageGuide):
                dialog.select_topic('failure')
            if isinstance(dialog, (HelpDialog, UsageGuide)):
                captured.append(dialog.content.toPlainText())
            if dialog:
                dialog.accept()
        for button in (self.window.quick_help, self.window.cookie_file_help, self.window.login_help):
            QTimer.singleShot(0, capture)
            button.click()
        self.assertEqual(len(captured), 3)
        self.assertIn('重试未完成', captured[0])
        self.assertIn('Ctrl+J', captured[1])
        self.assertIn('数据库', captured[1])
        self.assertIn('edge://version', captured[2])
        self.assertNotIn('.md', ''.join(captured))

    def test_hover_hints_and_collapsible_proxy_keep_values(self):
        for widget in (self.window.url, self.window.read_cookies, self.window.cookies, self.window.profile_picker,
                       self.window.history_button, self.window.cancel_button, self.window.quick_help):
            self.assertTrue(widget.toolTip())
        self.assertTrue(self.window.proxy.isHidden())
        self.window.proxy_toggle.click()
        self.assertFalse(self.window.proxy.isHidden())
        self.window.proxy.setText('http://127.0.0.1:1234')
        self.window.proxy_toggle.click()
        self.assertTrue(self.window.proxy.isHidden())
        self.assertEqual(self.window.network_settings().proxy, 'http://127.0.0.1:1234')

    def test_import_validates_immediately_and_preserves_previous_on_error(self):
        valid = self.root / 'cookies.txt'
        valid.write_text('# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic\n')
        self.window.url.setText('https://www.bilibili.com/video/test')
        with patch('video_demo.gui.QFileDialog.getOpenFileName', return_value=(str(valid), '')):
            self.window.browse_cookies()
        self.assertIn('1 条', self.window.cookie_status.text())
        bad = self.root / 'Cookies'
        bad.write_bytes(b'SQLite format 3\x00bad')
        with patch('video_demo.gui.QFileDialog.getOpenFileName', return_value=(str(bad), '')), patch('video_demo.gui.QMessageBox.information') as message:
            self.window.browse_cookies()
        self.assertIn('数据库', message.call_args.args[2])
        self.assertEqual(self.window.cookies.text(), str(valid))


if __name__ == '__main__':
    unittest.main()
