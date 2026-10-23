import re
import tempfile
import threading
import unittest
from pathlib import Path

from tests.integration_demo import FIXTURES, ffmpeg, make_fixtures
from video_demo.core import convert_mp4, inspect_local_media


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_fixtures()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="media-test-")
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_ten_bit_h264_becomes_compatible_eight_bit_mp4(self):
        source = self.folder / "ten-bit.mp4"
        ffmpeg("-i", FIXTURES / "first.mp4", "-t", "1", "-vf", "format=yuv420p10le",
               "-c:v", "libx264", "-profile:v", "high10", "-c:a", "copy", source)
        self.assertEqual(inspect_local_media(source)["pixel_format"], "yuv420p10le")
        target = self.folder / "compatible.mp4"
        convert_mp4(source, target, threading.Event(), lambda *_: None)
        media = inspect_local_media(target)
        self.assertEqual(media["video_codec"], "h264")
        self.assertEqual(media["audio_codec"], "aac")
        self.assertEqual(media["pixel_format"], "yuv420p")
        ffmpeg("-v", "error", "-xerror", "-i", target, "-f", "null", "-")

    def test_compatible_source_preserves_decoded_frames(self):
        source = FIXTURES / "first.mp4"
        target = self.folder / "remux.mp4"
        convert_mp4(source, target, threading.Event(), lambda *_: None)
        def hashes(path):
            data = ffmpeg("-v", "error", "-i", path, "-an", "-f", "framemd5", "-").stdout.decode()
            return re.findall(r"[0-9a-f]{32}$", data, re.M)
        original, result = hashes(source), hashes(target)
        self.assertTrue(original)
        self.assertEqual(original, result)

    def test_full_range_h264_is_rescaled_and_checked_as_limited_range(self):
        source = self.folder / "full-range.mp4"
        ffmpeg("-i", FIXTURES / "first.mp4", "-t", "1", "-vf", "scale=in_range=tv:out_range=pc",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "pc", "-c:a", "copy", source)
        self.assertEqual(inspect_local_media(source)["color_range"], "pc")
        target = self.folder / "limited-range.mp4"
        result = convert_mp4(source, target, threading.Event(), lambda *_: None)
        self.assertEqual(result["pixel_format"], "yuv420p")
        self.assertNotEqual(result["color_range"], "pc")
        self.assertEqual(result, inspect_local_media(target))
        ffmpeg("-v", "error", "-xerror", "-err_detect", "explode", "-i", target, "-f", "null", "-")

    def test_conversion_returns_actual_padded_output_size(self):
        source = self.folder / "odd-size.mkv"
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=319x179:rate=5", "-t", "1",
               "-c:v", "ffv1", "-pix_fmt", "yuv444p", source)
        self.assertEqual(inspect_local_media(source)["height"], 179)
        target = self.folder / "even-size.mp4"
        result = convert_mp4(source, target, threading.Event(), lambda *_: None)
        self.assertEqual((result["width"], result["height"]), (320, 180))
        self.assertEqual(result["pixel_format"], "yuv420p")


if __name__ == "__main__":
    unittest.main()
