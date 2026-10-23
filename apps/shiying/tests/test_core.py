import threading
import unittest
from unittest.mock import patch
import yt_dlp

from video_demo.core import Cancelled, Video, analyze, check_cancel, discover_html, format_selector, safe_stem, validate_url


class CoreTests(unittest.TestCase):
    def test_url_guard(self):
        self.assertEqual(validate_url(" https://example.com/a?q=1#x "), "https://example.com/a?q=1")
        for value in ("file:///C:/test", "not a url", "ftp://example.com/a", "https://user:pass@example.com", "https://example.com:bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_url(value)

    def test_sources_are_grouped_and_links_deduplicated(self):
        html = '''<base href="/media/"><video title="one"><source src="low.mp4"><source src="high.mp4"></video>
        <a href="low.mp4">duplicate</a><a href="/two.m3u8?token=x">video</a>
        <iframe src="https://example.org/player"></iframe><a href="javascript:alert(1)">bad</a>'''
        found = discover_html(html, "https://example.com/page/index.html")
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0].urls, ["https://example.com/media/low.mp4", "https://example.com/media/high.mp4"])
        self.assertEqual(found[0].title, "one")
        self.assertEqual(found[1].urls, ["https://example.com/two.m3u8?token=x"])

    def test_drm_audio_and_storyboards_excluded(self):
        info = {"title": "demo", "formats": [
            {"height": 1080, "vcodec": "h264", "has_drm": True},
            {"height": 720, "vcodec": "h264"},
            {"height": 720, "vcodec": "vp9"},
            {"height": 360, "ext": "mhtml"},
            {"vcodec": "none", "acodec": "aac"}]}
        video = Video.from_info(info, "https://example.com")
        self.assertEqual(video.heights, [720])
        with self.assertRaises(ValueError):
            Video.from_info({"has_drm": True}, "https://example.com")

    def test_height_applies_to_every_fallback(self):
        for branch in format_selector(720).split("/"):
            self.assertIn("[height<=720]", branch)
        self.assertNotIn("height", format_selector(None))

    def test_windows_filenames(self):
        self.assertEqual(safe_stem('a:b/c\\d?'), "a_b_c_d_")
        self.assertEqual(safe_stem("CON"), "_CON")
        self.assertEqual(safe_stem("..."), "video")

    def test_cancel(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            check_cancel(event)

    def test_independent_videos_with_shared_audio_are_preserved(self):
        audio = {"url": "https://example.com/shared.m4a", "vcodec": "none", "acodec": "aac"}
        first = {"id": "one", "title": "First", "extractor_key": "Fixture", "formats": [
            {"url": "https://example.com/one.mp4", "vcodec": "h264", "height": 720}, audio]}
        second = {"id": "two", "title": "Second", "extractor_key": "Fixture", "formats": [
            {"url": "https://example.com/two.mp4", "vcodec": "h264", "height": 720}, audio]}
        playlist = {"_type": "playlist", "entries": [first, second, first]}
        with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value=playlist):
            videos = analyze("https://example.com/videos", threading.Event(), lambda *_: None)
        self.assertEqual([v.title for v in videos], ["First", "Second"])


if __name__ == "__main__":
    unittest.main()
