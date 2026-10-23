"""Real Range/If-Range transfers, changed resources and interrupted conversion."""
import http.server
import functools
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.integration_demo import FIXTURES, Handler, ffmpeg, inspect, make_fixtures
from video_demo.core import Cancelled, Network, Video, analyze, download
from video_demo.resume import CACHE_NAME, ProgressBusy, clear_progress, list_progress, progress_lock


class RangeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        data = server.assets.get(self.path, server.payload)
        etag = server.tags.get(self.path, server.etag)
        ranged = self.headers.get("Range", "")
        conditional = self.headers.get("If-Range", "")
        server.requests.append({"range": ranged, "if_range": conditional, "path": self.path})
        start, end, status = 0, len(data) - 1, 200
        matched = re.fullmatch(r"bytes=(\d+)-(\d*)", ranged)
        if matched and server.supports_range and (not conditional or conditional == etag):
            start, status = int(matched[1]), 206
            if matched[2]:
                end = min(int(matched[2]), end)
            if start >= len(data):
                self.send_error(416)
                return
        if server.change_during_resume and matched and int(matched[1]) > 0:
            server.change_during_resume = False
            server.etag = etag = '"changed-during-resume"'
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(end - start + 1))
        if etag:
            self.send_header("ETag", etag)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        try:
            stop = min(end + 1, start + server.fail_after) if getattr(server, "fail_after", 0) else end + 1
            for offset in range(start, stop, 32768):
                self.wfile.write(data[offset:min(offset + 32768, stop)])
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_fixtures()
        cls.alternate = FIXTURES / "resume-alternate.mp4"
        if not cls.alternate.exists():
            ffmpeg("-i", FIXTURES / "first.mp4", "-vf", "hflip", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy", cls.alternate)
        cls.silent = FIXTURES / "resume-video-only.mp4"
        cls.audio = FIXTURES / "resume-audio-only.m4a"
        if not cls.silent.exists():
            ffmpeg("-i", FIXTURES / "first.mp4", "-map", "0:v:0", "-c", "copy", cls.silent)
        if not cls.audio.exists():
            ffmpeg("-i", FIXTURES / "first.mp4", "-map", "0:a:0", "-c", "copy", cls.audio)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        self.server.payload = (FIXTURES / "first.mp4").read_bytes()
        self.server.etag = '"initial-content"'
        self.server.supports_range = True
        self.server.change_during_resume = False
        self.server.requests = []
        self.server.assets = {}
        self.server.tags = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/asset.mp4"
        self.events = []
        self.notify = lambda kind, data: self.events.append((kind, data))
        self.video = analyze(self.url, threading.Event(), self.notify)[0]
        self.server.requests.clear()
        self.network = Network(keep_progress=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def interrupt(self):
        cancel = threading.Event()
        def stop(kind, data):
            self.notify(kind, data)
            if kind == "progress" and data.get("phase") == "下载" and (data.get("percent") or 0) > 20:
                cancel.set()
        with self.assertRaises(Cancelled):
            download(self.video, None, self.folder, cancel, stop, self.network)
        self.assertFalse(list(self.folder.glob("*.mp4")))

    def test_cancel_preserves_progress_and_new_analysis_resumes_with_if_range(self):
        self.interrupt()
        records = list_progress(self.folder)
        self.assertEqual(len(records), 1)
        self.assertGreater(records[0]["bytes"], 100000)
        manifest = next((self.folder / CACHE_NAME).glob("*/progress.json")).read_text(encoding="utf-8")
        self.assertNotIn(self.url, manifest)
        self.assertNotIn("Cookie", manifest)
        fresh = analyze(self.url, threading.Event(), self.notify)[0]
        self.assertNotEqual(fresh.key, self.video.key)
        self.server.requests.clear()
        result = download(fresh, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        self.assertTrue(any(int(re.match(r"bytes=(\d+)", r["range"])[1]) > 0 and r["if_range"] == '"initial-content"'
                            for r in self.server.requests if r["range"]))
        self.assertEqual(list_progress(self.folder), [])
        self.assertTrue(any(d.get("resumed") for k, d in self.events if k == "progress"))

    def test_changed_content_restarts_and_decodes_new_video(self):
        self.interrupt()
        self.server.payload = self.alternate.read_bytes()
        self.server.etag = '"new-content"'
        self.server.requests.clear()
        result = download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        self.assertFalse(any(r["range"].startswith("bytes=1") and r["range"] != "bytes=0-0" for r in self.server.requests))
        def frames(path):
            text = ffmpeg("-v", "error", "-i", path, "-an", "-f", "framemd5", "-").stdout.decode()
            return re.findall(r"[0-9a-f]{32}$", text, re.M)
        self.assertEqual(frames(result), frames(self.alternate))
        self.assertTrue(any("已变化" in d["text"] for k, d in self.events if k == "log"))

    def test_server_without_ranges_restarts_without_corrupting_output(self):
        self.interrupt()
        self.server.supports_range = False
        self.server.requests.clear()
        result = download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        self.assertEqual(self.server.requests[0]["range"], "bytes=0-0")
        self.assertEqual(self.server.requests[-1]["range"], "")
        self.assertEqual(list_progress(self.folder), [])

    def test_server_without_strong_validator_does_not_keep_unusable_partials(self):
        self.server.etag = 'W/"weak-content"'
        self.interrupt()
        self.assertEqual(list_progress(self.folder), [])
        self.server.etag = None
        self.interrupt()
        self.assertEqual(list_progress(self.folder), [])

    def test_interrupted_mp4_conversion_reuses_complete_source_without_network(self):
        def stopped(*args, **kwargs):
            raise Cancelled("Stop conversion")
        with patch("video_demo.core.convert_mp4", side_effect=stopped):
            with self.assertRaises(Cancelled):
                download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        self.assertEqual(len(list_progress(self.folder)), 1)
        self.server.requests.clear()
        result = download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(list_progress(self.folder), [])

    def test_modified_local_partial_is_discarded_before_resuming(self):
        self.interrupt()
        partial = next((self.folder / CACHE_NAME).glob("*/*.part"))
        with partial.open("ab") as stream:
            stream.write(b"changed by another program")
        self.server.requests.clear()
        result = download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        self.assertFalse(any(r["range"] for r in self.server.requests))

    def test_changed_during_resume_is_restarted_once(self):
        self.interrupt()
        self.server.change_during_resume = True
        result = download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        self.assertTrue(any("传输中发现文件变化" in d["text"] for k, d in self.events if k == "log"))

    def test_cleanup_deletes_only_owned_progress_and_keeps_final_videos(self):
        self.interrupt()
        records = list_progress(self.folder)
        final = self.folder / "keep.mp4"
        final.write_bytes(b"unrelated local file")
        with self.assertRaises(ValueError):
            clear_progress(self.folder, ["../keep.mp4"])
        clear_progress(self.folder, [records[0]["key"]])
        self.assertEqual(final.read_bytes(), b"unrelated local file")
        self.assertEqual(list_progress(self.folder), [])

    def test_progress_lock_prevents_parallel_writes(self):
        with progress_lock(self.folder):
            with self.assertRaises(ProgressBusy):
                list_progress(self.folder)

    def test_interrupted_network_preserves_valid_partial_for_manual_retry(self):
        self.server.fail_after = 64 * 1024
        with self.assertRaises(Exception):
            download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        self.assertEqual(len(list_progress(self.folder)), 1)
        self.server.fail_after = 0
        self.server.requests.clear()
        result = download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        self.assertTrue(any(r["range"] not in ("", "bytes=0-0") for r in self.server.requests))

    def test_hls_cancel_restarts_but_complete_source_can_continue_conversion(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/master.m3u8"
            item = analyze(url, threading.Event(), self.notify)[0]
            cancel = threading.Event()
            def stopped(kind, data):
                if kind == "progress" and data.get("phase") == "下载":
                    cancel.set()
            with self.assertRaises(Cancelled):
                download(item, 180, self.folder, cancel, stopped, self.network)
            self.assertEqual(list_progress(self.folder), [])
            with patch("video_demo.core.convert_mp4", side_effect=Cancelled()):
                with self.assertRaises(Cancelled):
                    download(item, 180, self.folder, threading.Event(), self.notify, self.network)
            self.assertEqual(len(list_progress(self.folder)), 1)
            with patch("video_demo.resume.ResumeYoutubeDL.urlopen", side_effect=AssertionError("A ready HLS source should not download again")):
                result = download(item, 180, self.folder, threading.Event(), self.notify, self.network)
            inspect(result)
            self.assertEqual(list_progress(self.folder), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def separate_video(self):
        base = self.url.rsplit("/", 1)[0]
        self.server.assets = {"/video.mp4": self.silent.read_bytes(), "/audio.m4a": self.audio.read_bytes()}
        self.server.tags = {"/video.mp4": '"video-track"', "/audio.m4a": '"audio-track"'}
        self.video = Video.from_info({"id": "separate", "title": "Separate audio and video", "extractor_key": "Fixture", "duration": 3,
            "formats": [
                {"format_id": "audio", "url": base + "/audio.m4a", "ext": "m4a", "protocol": "http", "vcodec": "none", "acodec": "aac"},
                {"format_id": "video", "url": base + "/video.mp4", "ext": "mp4", "protocol": "http", "vcodec": "h264", "acodec": "none", "height": 360},
            ]}, base + "/watch")

    def interrupt_audio(self):
        cancel = threading.Event()
        finished = []
        def stop(kind, data):
            self.notify(kind, data)
            if kind == "progress" and data.get("phase") == "下载完成，准备合并":
                finished.append(True)
            if finished and kind == "progress" and data.get("phase") == "下载" and (data.get("percent") or 0) > 10:
                cancel.set()
        with self.assertRaises(Cancelled):
            download(self.video, None, self.folder, cancel, stop, self.network)

    def test_completed_video_and_partial_audio_resume_and_merge_correctly(self):
        self.separate_video()
        self.interrupt_audio()
        self.assertEqual(len(list_progress(self.folder)), 1)
        self.server.requests.clear()
        result = download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        video_requests = [r for r in self.server.requests if r["path"] == "/video.mp4"]
        self.assertEqual(video_requests, [{"path": "/video.mp4", "range": "bytes=0-0", "if_range": '"video-track"'}])
        self.assertTrue(any(r["path"] == "/audio.m4a" and r["range"] not in ("", "bytes=0-0") and r["if_range"] == '"audio-track"' for r in self.server.requests))
        self.assertEqual(list_progress(self.folder), [])

    def test_changed_completed_video_does_not_get_merged_with_old_audio(self):
        self.separate_video()
        self.interrupt_audio()
        self.server.tags["/video.mp4"] = '"new-video-track"'
        self.server.requests.clear()
        result = download(self.video, None, self.folder, threading.Event(), self.notify, self.network)
        inspect(result)
        self.assertTrue(any(r["path"] == "/video.mp4" and not r["range"] for r in self.server.requests))
        self.assertTrue(any(r["path"] == "/audio.m4a" and not r["range"] for r in self.server.requests))


if __name__ == "__main__":
    unittest.main()
