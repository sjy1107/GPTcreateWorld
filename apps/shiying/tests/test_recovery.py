"""Real HTTP tests for expired links, bounded retries and video identity."""
import functools
import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tests.integration_demo import FIXTURES, inspect, make_fixtures
from video_demo.core import Cancelled, analyze, download


class ExpiringHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/expiring.html":
            resource = self.server.resource
            body = (f'<html><head><title>Expiry test</title></head><body>'
                    f'<video src="/{resource}?token={self.server.token}" controls></video>'
                    '</body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts.path in ("/signed.mp4", "/changed.mp4"):
            token = parse_qs(parts.query).get("token", [""])[0]
            self.server.attempts.append((parts.path, token))
            if token != str(self.server.token) or self.server.deny_all:
                self.send_error(self.server.denial_status, "Expired media URL")
                return
            self.path = "/first.mp4"
        return super().do_GET()


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_fixtures()

    def setUp(self):
        handler = functools.partial(ExpiringHandler, directory=str(FIXTURES))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.token = 1
        self.server.resource = "signed.mp4"
        self.server.deny_all = False
        self.server.denial_status = 403
        self.server.attempts = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/expiring.html"
        self.cancel = threading.Event()
        self.events = []
        self.notify = lambda kind, data: self.events.append((kind, data))
        self.temp = tempfile.TemporaryDirectory(prefix="recovery-test-")
        self.output = Path(self.temp.name)
        self.video = analyze(self.url, self.cancel, self.notify)[0]
        self.server.attempts.clear()
        self.server.token = 2

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_expired_link_refreshes_and_decodes(self):
        result = download(self.video, None, self.output, self.cancel, self.notify)
        inspect(result)
        tokens = [token for _, token in self.server.attempts]
        self.assertIn("1", tokens)
        self.assertIn("2", tokens)
        self.assertEqual(len(list(self.output.glob("*.mp4"))), 1)
        self.assertFalse(list(self.output.glob(".video-work-*")))

    def test_repeated_denial_stops_after_one_refresh(self):
        self.server.deny_all = True
        self.server.denial_status = 410
        with self.assertRaisesRegex(RuntimeError, "已重新解析并重试一次"):
            download(self.video, None, self.output, self.cancel, self.notify)
        self.assertEqual(len(self.server.attempts), 2)
        self.assertFalse(list(self.output.iterdir()))

    def test_changed_video_is_not_downloaded(self):
        self.server.resource = "changed.mp4"
        with self.assertRaisesRegex(RuntimeError, "视频已变化|原视频"):
            download(self.video, None, self.output, self.cancel, self.notify)
        self.assertFalse(any(path == "/changed.mp4" for path, _ in self.server.attempts))
        self.assertFalse(list(self.output.iterdir()))

    def test_cancel_before_refresh_does_not_retry(self):
        def cancel_on_refresh(kind, data):
            self.notify(kind, data)
            if kind == "progress" and data.get("phase") == "刷新视频地址":
                self.cancel.set()
        with self.assertRaises(Cancelled):
            download(self.video, None, self.output, self.cancel, cancel_on_refresh)
        self.assertEqual(len(self.server.attempts), 1)
        self.assertFalse(list(self.output.iterdir()))


if __name__ == "__main__":
    unittest.main()
