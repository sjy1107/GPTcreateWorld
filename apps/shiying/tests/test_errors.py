import errno
import unittest
from unittest.mock import patch

from video_demo.errors import explain_error, redact_detail


class ErrorHintTests(unittest.TestCase):
    def test_common_failures_have_specific_guidance(self):
        cases = [
            (RuntimeError("HTTP Error 403: Forbidden"), "access_denied"),
            (RuntimeError("404 Client Error: Not Found"), "unavailable"),
            (RuntimeError("HTTP Error 410: Gone"), "unavailable"),
            (RuntimeError("HTTP Error 429: Too Many Requests"), "rate_limited"),
            (RuntimeError("HTTP Error 474: Client Error"), "request_rejected"),
            (RuntimeError("HTTP 503 Service Unavailable"), "server"),
            (RuntimeError("HTTP Error 407: Proxy Authentication Required"), "proxy"),
            (TimeoutError("Connection timed out"), "timeout"),
            (ConnectionRefusedError("Connection refused"), "connection"),
            (RuntimeError("getaddrinfo failed"), "dns"),
            (RuntimeError("certificate verify failed"), "tls"),
            (OSError(errno.ENOSPC, "No space left on device"), "disk_full"),
            (PermissionError(errno.EACCES, "Permission denied"), "save_permission"),
            (RuntimeError("MP4 转换失败：Invalid data found"), "conversion"),
            (RuntimeError("Requested format is not available"), "quality_unavailable"),
            (RuntimeError("This video has DRM"), "protected"),
            (RuntimeError("Fresh cookies (not necessarily logged in) are needed"), "session_required"),
            (RuntimeError("Instagram: login required"), "authentication"),
            (RuntimeError("You need to log in to access this content"), "authentication"),
            (RuntimeError("NSFW tweet requires authentication"), "authentication"),
            (RuntimeError("The provided Instagram account cookies are no longer valid"), "session_expired"),
            (RuntimeError("HTTP Error 401: Unauthorized"), "authentication"),
            (RuntimeError("This content isn't available to everyone: It can't be seen by certain audiences."), "audience_restricted"),
            (RuntimeError("Unsupported URL"), "not_detected"),
            (RuntimeError("No ffmpeg exe could be found"), "dependency"),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                hint = explain_error(error)
                self.assertEqual(hint.code, expected)
                self.assertTrue(hint.reason)
                self.assertTrue(hint.actions)
                self.assertIn("建议", hint.as_text())

    def test_video_id_containing_http_status_digits_is_not_a_status(self):
        hint = explain_error(ValueError("Unknown resource https://example.com/watch?id=403410429"))
        self.assertEqual(hint.code, "unknown")
        self.assertIsNone(hint.status)

    def test_unavailable_message_is_actionable_without_inventing_deletion_or_http_status(self):
        hint = explain_error(RuntimeError('ERROR: [youtube] sample: This video is unavailable'), 'analyze')
        self.assertEqual(hint.code, 'unavailable')
        self.assertIsNone(hint.status)
        self.assertIn('不能仅凭', hint.reason)
        self.assertTrue(hint.actions)
        url_only = explain_error(RuntimeError('Unsupported URL: https://example.com/video-unavailable'))
        self.assertEqual(url_only.code, 'not_detected')
        server = explain_error(RuntimeError('HTTP Error 503: Service Unavailable; Video unavailable'))
        self.assertEqual(server.code, 'server')

    def test_socket_access_denial_is_not_reported_as_a_disk_problem(self):
        error = PermissionError("WinError 10013: socket access denied")
        error.winerror = 10013
        self.assertEqual(explain_error(error).code, "connection")

    def test_wrapped_http_status_and_changed_quality(self):
        try:
            raise RuntimeError("HTTP Error 403: Forbidden")
        except RuntimeError as original:
            wrapped = RuntimeError("媒体下载失败")
            wrapped.__cause__ = original
            self.assertEqual(explain_error(wrapped).status, 403)
            changed = ValueError("刷新后所选清晰度已不可用")
            changed.__cause__ = original
            self.assertEqual(explain_error(changed).code, "quality_unavailable")

    def test_sensitive_headers_and_signed_urls_are_not_in_details(self):
        detail = redact_detail("Failed https://user:password@example.com/private.mp4?token=secret\nCookie: session=abc\nAuthorization: Bearer xyz")
        for secret in ("password", "private.mp4", "secret", "session=abc", "Bearer xyz"):
            self.assertNotIn(secret, detail)
        self.assertIn("example.com", detail)

    def test_worker_reports_temp_directory_failure(self):
        from video_demo.gui import Worker
        worker = Worker("analyze", "https://example.com/video")
        with patch("video_demo.gui.tempfile.TemporaryDirectory", side_effect=PermissionError("Permission denied")):
            worker.run()
        self.assertEqual(worker.error_hint.code, "save_permission")
        self.assertTrue(worker.error)
        self.assertIsNone(worker.result)


if __name__ == "__main__":
    unittest.main()
