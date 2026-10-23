"""Self-contained, real HTTP + yt-dlp + FFmpeg integration check.

Creates synthetic media, serves a local page and validates decoded MP4 outputs.
No external video website or copyrighted test content is required.
"""
import functools
import http.server
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

from video_demo.core import CREATE_NO_WINDOW, Cancelled, analyze, download, ffmpeg_path

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
OUTPUT = ROOT / "output"


def ffmpeg(*args):
    result = subprocess.run([ffmpeg_path(), "-hide_banner", "-nostdin", "-y", *map(str, args)],
                            capture_output=True, creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result


def make_fixtures():
    FIXTURES.mkdir(exist_ok=True)
    OUTPUT.mkdir(exist_ok=True)
    if not (FIXTURES / "first.mp4").exists():
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
               "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", FIXTURES / "first.mp4")
        ffmpeg("-i", FIXTURES / "first.mp4", "-vf", "scale=320:180", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
               "-c:a", "libopus", FIXTURES / "second.webm")
        for height in (180, 360):
            ffmpeg("-i", FIXTURES / "first.mp4", "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "ultrafast",
                   "-g", "24", "-c:a", "aac", "-f", "hls", "-hls_time", "1", "-hls_list_size", "0", FIXTURES / f"hls_{height}.m3u8")
    if not (FIXTURES / "init-stream0.m4s").exists():
        ffmpeg("-i", FIXTURES / "first.mp4", "-c", "copy", "-f", "dash", "-seg_duration", "1",
               "-init_seg_name", str(FIXTURES / "init-stream$RepresentationID$.m4s"),
               "-media_seg_name", str(FIXTURES / "chunk-stream$RepresentationID$-$Number%05d$.m4s"),
               "-adaptation_sets", "id=0,streams=v id=1,streams=a", FIXTURES / "manifest.mpd")
        # DASH file references must be URLs relative to the HTTP manifest.
        manifest = FIXTURES / "manifest.mpd"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(str(FIXTURES) + "\\", "").replace(str(FIXTURES) + "/", ""), encoding="utf-8")
    (FIXTURES / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x180\nhls_180.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360\nhls_360.m3u8\n", encoding="utf-8")
    shutil.copyfile(FIXTURES / "first.mp4", FIXTURES / "linked.mp4")
    (FIXTURES / "index.html").write_text('''<!doctype html><html><head><meta charset="UTF-8"><title>拾影功能测试</title></head>
    <body><h1>本地视频测试页</h1>
    <video controls title="MP4 测试"><source src="first.mp4" type="video/mp4"></video>
    <video controls title="WebM 转码测试"><source src="second.webm" type="video/webm"></video>
    <video controls title="HLS 清晰度测试"><source src="master.m3u8" type="application/x-mpegURL"></video>
    <video controls title="DASH 音视频合并"><source src="manifest.mpd" type="application/dash+xml"></video>
    <a href="first.mp4">重复文件链接</a><a href="linked.mp4">仅通过链接提供的视频</a></body></html>''', encoding="utf-8")


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def inspect(path):
    probe = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, creationflags=CREATE_NO_WINDOW)
    text = probe.stderr.decode("utf-8", errors="replace")
    assert "Video: h264" in text, text
    assert "Audio: aac" in text, text
    ffmpeg("-v", "error", "-xerror", "-err_detect", "explode", "-i", path, "-f", "null", "-")
    return text


def main():
    make_fixtures()
    handler = functools.partial(Handler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    messages = []
    notify = lambda kind, data: messages.append((kind, data))
    results = []
    try:
        videos = analyze(url, threading.Event(), notify)
        assert len(videos) == 5, [(v.title, v.heights) for v in videos]
        hls = next(v for v in videos if 180 in v.heights and 360 in v.heights)
        assert hls.heights == [360, 180], hls.heights
        for video in videos:
            height = 180 if video.key == hls.key else None
            path = download(video, height, OUTPUT, threading.Event(), notify)
            details = inspect(path)
            if height:
                assert re.search(r"320x180", details), details
            results.append({"file": str(path), "height": height, "bytes": path.stat().st_size})

        # Force the main page extractor to fail: tag discovery must still find
        # independent videos, with no duplicates for the repeated MP4 link.
        import yt_dlp
        original = yt_dlp.YoutubeDL.extract_info

        def fail_page(self, requested, *args, **kwargs):
            if requested == url:
                raise ValueError("Simulated unsupported webpage")
            return original(self, requested, *args, **kwargs)

        with patch.object(yt_dlp.YoutubeDL, "extract_info", fail_page):
            fallback = analyze(url, threading.Event(), notify)
        assert len(fallback) == 5, [v.title for v in fallback]

        # Cancel during actual network download and confirm no final MP4 is published.
        cancel = threading.Event()
        before = set(OUTPUT.iterdir())

        def cancel_on_progress(kind, data):
            if kind == "progress" and data.get("phase") == "下载":
                cancel.set()

        try:
            download(videos[0], None, OUTPUT, cancel, cancel_on_progress)
        except Exception:
            assert cancel.is_set()
        else:
            raise AssertionError("Cancelled download unexpectedly succeeded")
        assert set(OUTPUT.iterdir()) == before, "Cancelled task left files"

        # Failed request followed by retry: restoring the fixture makes the same
        # analyzed video downloadable again.
        first = FIXTURES / "first.mp4"
        hidden = FIXTURES / "first.retry-test.mp4"
        mp4_video = next(v for v in videos if any(f.get("url", "").endswith("first.mp4") for f in v.info["formats"]))
        first.rename(hidden)
        try:
            try:
                download(mp4_video, None, OUTPUT, threading.Event(), notify)
            except Exception:
                pass
            else:
                raise AssertionError("Missing resource did not fail")
        finally:
            hidden.rename(first)
        retry = download(mp4_video, None, OUTPUT, threading.Event(), notify)
        inspect(retry)
        assert not list(OUTPUT.glob(".video-work-*")), "Temporary fragments remain"
        assert any(k == "progress" and d.get("speed") for k, d in messages), "Missing speed updates"
        report = {"result": "passed", "videos": len(videos), "fallback_videos": len(fallback),
                  "formats": ["MP4", "WebM to MP4", "HLS 180p", "DASH separate audio/video"],
                  "cancellation": "passed", "retry": "passed", "files": results}
        (ROOT / "integration_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
