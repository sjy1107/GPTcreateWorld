"""Use synthetic browser databases and credentials; never access user profiles."""
import http.cookiejar
from contextlib import closing
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_dlp.cookies import YoutubeDLCookieJar

from video_demo.browser_cookies import BrowserCookieError, BrowserSession, read_browser_session, site_scope
from video_demo.core import Cancelled, Network
from video_demo.errors import explain_error
from video_demo.gui import Worker


def fake_cookie(domain=".instagram.com", expires=None):
    return http.cookiejar.Cookie(0, "demo_session", "synthetic_value", None, False,
                                domain, True, True, "/", True, True, expires, True, None, None, {})


class BrowserCookieTests(unittest.TestCase):
    def test_real_firefox_reader_filters_unrelated_and_expired_cookies(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cookies.sqlite"
            future = int(time.time() + 3600) * 1000
            past = int(time.time() - 3600) * 1000
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA user_version=16")
                db.execute("CREATE TABLE moz_cookies(host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)")
                db.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?, '/', ?, 1)", [
                    (".instagram.com", "valid", "synthetic_current", future),
                    (".instagram.com", "expired", "synthetic_old", past),
                    (".example.com", "unrelated", "synthetic_other", future),
                    (".instagram.com.evil.example", "lookalike", "synthetic_wrong", future),
                ])
                db.commit()
            original = path.read_bytes()
            session = read_browser_session("firefox", folder, "https://www.instagram.com/reel/demo/", lambda: None)
            self.assertEqual([cookie.name for cookie in session.cookies], ["valid"])
            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn("synthetic_current", repr(session))

    def test_site_groups_and_domain_boundaries(self):
        session = BrowserSession("firefox", site_scope("https://x.com/example/status/1"), ())
        self.assertTrue(session.supports("https://twitter.com/example/status/1"))
        for url in ("https://instagram.com/reel/demo", "https://notx.com", "https://x.com.evil.example", ""):
            self.assertFalse(session.supports(url))
        self.assertEqual(site_scope("https://www.example.org/video"), ("example.org",))

    def test_temporary_task_file_is_scoped_and_does_not_mutate_session(self):
        cookie = fake_cookie()
        session = BrowserSession("firefox", ("instagram.com",), (cookie, fake_cookie(".unrelated.example")))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cookies.txt"
            session.write_task_file(path)
            jar = YoutubeDLCookieJar(str(path))
            jar.load()
            self.assertEqual(len(jar), 1)
            self.assertEqual(next(iter(jar)).value, "synthetic_value")
            self.assertIsNone(cookie.expires)
            self.assertNotIn("unrelated.example", path.read_text())

    def test_all_expired_session_stops_before_network(self):
        session = BrowserSession("firefox", ("instagram.com",), (fake_cookie(expires=time.time() - 10),))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cookies.txt"
            with self.assertRaises(BrowserCookieError) as caught:
                session.write_task_file(path)
            self.assertEqual(caught.exception.code, "browser_cookie_expired")
            self.assertFalse(path.exists())

    def test_decryption_missing_and_partial_results_are_explicit(self):
        for error, code in ((RuntimeError("failed to decrypt with DPAPI"), "encrypted"),
                            (PermissionError("locked"), "locked"), (FileNotFoundError("missing"), "missing")):
            with self.subTest(code=code), patch("video_demo.browser_cookies.extract_cookies_from_browser", side_effect=error):
                with self.assertRaises(BrowserCookieError) as caught:
                    read_browser_session("chrome", "Default", "https://instagram.com/", lambda: None)
                self.assertEqual(explain_error(caught.exception).code, "browser_cookie_" + code)
        def partial(browser, profile, logger):
            logger.warning("failed to decrypt with DPAPI", only_once=True)
            jar = YoutubeDLCookieJar()
            jar.set_cookie(fake_cookie())
            return jar
        with patch("video_demo.browser_cookies.extract_cookies_from_browser", side_effect=partial):
            self.assertTrue(read_browser_session("chrome", "", "https://instagram.com/", lambda: None).partial)

    def test_empty_site_and_cancellation_are_not_success(self):
        with patch("video_demo.browser_cookies.extract_cookies_from_browser", return_value=YoutubeDLCookieJar()) as reader:
            with self.assertRaises(BrowserCookieError) as caught:
                read_browser_session("firefox", "", "https://instagram.com/", lambda: None)
            self.assertEqual(caught.exception.code, "browser_cookie_empty")
            reader.reset_mock()
            def cancel():
                raise Cancelled()
            with self.assertRaises(Cancelled):
                read_browser_session("firefox", "", "https://instagram.com/", cancel)
            reader.assert_not_called()

    def test_worker_uses_and_removes_private_task_copy(self):
        session = BrowserSession("firefox", ("instagram.com",), (fake_cookie(),))
        worker = Worker("analyze", "https://instagram.com/reel/demo", Network(), session)
        captured = []
        def analyze(url, cancel, notify, network):
            path = Path(network.cookie_file)
            self.assertTrue(path.exists())
            captured.append(path)
            jar = YoutubeDLCookieJar(str(path))
            jar.load()
            self.assertEqual(len(jar), 1)
            return ["synthetic result"]
        with patch("video_demo.gui.analyze", side_effect=analyze):
            worker.run()
        self.assertFalse(worker.error)
        self.assertEqual(worker.result, ["synthetic result"])
        self.assertFalse(captured[0].exists())
        self.assertIsNone(session.cookies[0].expires)

    def test_public_resource_does_not_read_browser_or_require_cookie_file(self):
        worker = Worker("analyze", "https://example.com/public.mp4")
        with patch("video_demo.gui.read_browser_session") as reader, patch("video_demo.gui.analyze", return_value=["public"]) as analyze:
            worker.run()
        reader.assert_not_called()
        self.assertEqual(analyze.call_args.args[3].cookie_file, "")
        self.assertEqual(worker.result, ["public"])

    def test_expired_browser_session_falls_back_to_public_request(self):
        session = BrowserSession("firefox", ("instagram.com",), (fake_cookie(expires=time.time() - 10),))
        worker = Worker("analyze", "https://instagram.com/reel/public", Network(), session)
        with patch("video_demo.gui.analyze", return_value=["public"]) as analyze:
            worker.run()
        self.assertFalse(worker.error)
        self.assertEqual(analyze.call_args.args[3].cookie_file, "")


if __name__ == "__main__":
    unittest.main()
