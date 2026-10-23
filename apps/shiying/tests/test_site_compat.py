import functools
import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yt_dlp
from tests.integration_demo import FIXTURES, inspect, make_fixtures
from video_demo.core import Cancelled, analyze, download
from video_demo.errors import AnalysisError, explain_error
from video_demo.site_compat import DouyinPublicIE, playback_url, site_fallback


class SiteCompatibilityTests(unittest.TestCase):
    def test_douyin_overlay_only_converts_an_unambiguous_video_on_the_real_site(self):
        video_id = "6982497745948921092"
        for page in ("https://www.douyin.com/", "https://www.douyin.com/user/example"):
            self.assertEqual(playback_url(page + "?modal_id=" + video_id), "https://www.douyin.com/video/" + video_id)
        for url in ("https://douyin.com.evil.test/?modal_id=" + video_id,
                    "https://www.douyin.com/live/1?modal_id=" + video_id,
                    "https://www.douyin.com/video/1111111111111?modal_id=" + video_id,
                    "https://www.douyin.com/?modal_id=bad",
                    f"https://www.douyin.com/?modal_id={video_id}&modal_id=1234567890123"):
            self.assertEqual(playback_url(url), url)

    def test_fallback_scope_and_access_parameters_are_preserved(self):
        errors = [RuntimeError("The web client only works when logged-in")]
        result = site_fallback("https://vimeo.com/12345?h=synthetic_hash", errors)
        self.assertEqual(result, ("vimeo", "https://player.vimeo.com/video/12345?h=synthetic_hash", "12345"))
        for url in ("https://vimeo.com.evil.test/12345", "https://player.vimeo.com/video/12345",
                    "https://vimeo.com/12345/unlisted_hash", "https://vimeo.com/showcase/12345", "https://vimeo.com:8443/12345"):
            self.assertIsNone(site_fallback(url, errors))
        for error in ("HTTP Error 403", "HTTP Error 429", "This video is protected by a password"):
            self.assertIsNone(site_fallback("https://vimeo.com/12345", [RuntimeError(error)]))

    def test_failed_compatibility_attempt_is_bounded(self):
        error = RuntimeError("Fresh cookies (not necessarily logged in) are needed")
        with patch.object(yt_dlp.YoutubeDL, "extract_info", side_effect=error) as extract, \
                patch("video_demo.core.page_candidates", return_value=[]):
            with self.assertRaises(AnalysisError):
                analyze("https://www.douyin.com/video/6982497745948921092", threading.Event(), lambda *_: None)
        self.assertEqual(extract.call_count, 2)

    def test_no_compatibility_attempt_for_successful_main_extractor(self):
        info = {"id": "12345", "title": "clear", "extractor_key": "Vimeo",
                "formats": [{"url": "https://media.test/clear.mp4", "ext": "mp4", "vcodec": "h264", "height": 360}]}
        with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value=info) as extract:
            videos = analyze("https://vimeo.com/12345", threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 1)
        extract.assert_called_once()

    def test_password_restriction_replaces_generic_client_login_hint(self):
        with patch.object(yt_dlp.YoutubeDL, "extract_info", side_effect=[
            RuntimeError("The web client only works when logged-in"),
            RuntimeError("This video is protected by a password, use the --video-password option")]), \
                patch("video_demo.core.page_candidates", return_value=[]):
            with self.assertRaises(AnalysisError) as caught:
                analyze("https://vimeo.com/12345", threading.Event(), lambda *_: None)
        self.assertEqual(explain_error(caught.exception).code, "password_required")

    def test_different_video_returned_by_fallback_is_rejected(self):
        other = {"id": "99999", "title": "wrong video", "extractor_key": "Vimeo",
                 "formats": [{"url": "https://media.test/other.mp4", "vcodec": "h264"}]}
        with patch.object(yt_dlp.YoutubeDL, "extract_info", side_effect=[
            RuntimeError("The web client only works when logged-in"), other]), \
                patch("video_demo.core.page_candidates", return_value=[]):
            with self.assertRaises(AnalysisError) as caught:
                analyze("https://vimeo.com/12345", threading.Event(), lambda *_: None)
        self.assertIn("未返回原视频", str(caught.exception))
        self.assertEqual(explain_error(caught.exception).code, "source_changed")

    def test_cancel_prevents_compatibility_request(self):
        cancel = threading.Event()
        def fail(*_args, **_kwargs):
            cancel.set()
            raise RuntimeError("The web client only works when logged-in")
        with patch.object(yt_dlp.YoutubeDL, "extract_info", side_effect=fail) as extract:
            with self.assertRaises(Cancelled):
                analyze("https://vimeo.com/12345", cancel, lambda *_: None)
        extract.assert_called_once()

    def test_douyin_existing_access_and_account_cookies_are_not_overwritten(self):
        with yt_dlp.YoutubeDL({"quiet": True, "cachedir": False}) as ydl:
            ie = DouyinPublicIE(ydl)
            for name, value in (("passport_csrf_token", "synthetic-csrf"), ("ttwid", "synthetic-visit"),
                                ("UIFID_TEMP", "synthetic-uifid"), ("sessionid", "synthetic-account")):
                ie._set_cookie(".douyin.com", name, value)
            before = {name: cookie.value for name, cookie in ie._get_cookies("https://www.douyin.com/").items()}
            with patch.object(ie, "_request_webpage", side_effect=AssertionError("Existing visit records must be reused")), \
                    patch.object(ie, "_download_json", return_value={"aweme_detail": {"aweme_id": "6982497745948921092"}}) as api, \
                    patch.object(ie, "_parse_aweme_video_app", return_value={"id": "6982497745948921092"}):
                ie._real_extract("https://www.douyin.com/video/6982497745948921092")
            after = {name: cookie.value for name, cookie in ie._get_cookies("https://www.douyin.com/").items()}
            self.assertEqual(before, after)
            self.assertEqual(api.call_args.kwargs["query"]["uifid"], "synthetic-uifid")
            self.assertEqual(ie.ie_key(), "Douyin")

    def test_missing_anonymous_access_record_stops_without_calling_video_api(self):
        with yt_dlp.YoutubeDL({"quiet": True, "cachedir": False}) as ydl:
            ie = DouyinPublicIE(ydl)
            ie._set_cookie(".douyin.com", "ttwid", "synthetic-visit")
            with patch.object(ie, "_request_webpage", return_value=SimpleNamespace(headers={})), \
                    patch.object(ie, "_download_json") as api:
                with self.assertRaisesRegex(Exception, "没有返回可用的访问记录"):
                    ie._real_extract("https://www.douyin.com/video/6982497745948921092")
            api.assert_not_called()

    def test_vimeo_fallback_result_downloads_over_http_and_decodes(self):
        make_fixtures()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        main_url, player_url = "https://vimeo.com/12345", "https://player.vimeo.com/video/12345"
        media = f"http://127.0.0.1:{server.server_port}/first.mp4"
        info = {"id": "12345", "title": "clear sample", "extractor_key": "Vimeo", "duration": 3,
                "formats": [{"format_id": "clear", "url": media, "ext": "mp4", "protocol": "http",
                             "vcodec": "h264", "acodec": "aac", "width": 640, "height": 360}]}
        original, calls = yt_dlp.YoutubeDL.extract_info, []
        def extract(ydl, url, *args, **kwargs):
            calls.append(url)
            if url == main_url:
                ydl.params['logger'].error("The web client only works when logged-in")
                return None
            if url == player_url:
                return info
            return original(ydl, url, *args, **kwargs)
        try:
            with tempfile.TemporaryDirectory() as folder, patch.object(yt_dlp.YoutubeDL, "extract_info", extract):
                video = analyze(main_url, threading.Event(), lambda *_: None)[0]
                self.assertEqual(video.referer, main_url)
                output = download(video, 360, Path(folder), threading.Event(), lambda *_: None)
                inspect(output)
                self.assertEqual(calls, [main_url, player_url])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
