"""Technical validation for a user-supplied page, without displaying media content.

python -m tests.verify_page URL --download --height 360
"""
import argparse
import json
import subprocess
import threading
import time
from pathlib import Path

from video_demo.core import CREATE_NO_WINDOW, analyze, download, ffmpeg_path, inspect_local_media
from video_demo.errors import explain_error


def main():
    parser = argparse.ArgumentParser(description="验证视频页解析及 MP4 下载；不显示视频画面。")
    parser.add_argument("url")
    parser.add_argument("--download", action="store_true", help="实际下载第一个视频并完整解码校验")
    parser.add_argument("--height", type=int, help="选择解析结果提供的清晰度高度；默认使用最低档进行验证")
    parser.add_argument("--output", type=Path, default=Path("tests/site_outputs"))
    args = parser.parse_args()
    cancel = threading.Event()
    last_update = 0.0

    def notify(kind, data):
        nonlocal last_update
        if kind == "progress" and time.monotonic() - last_update >= 2:
            last_update = time.monotonic()
            print(json.dumps(data, ensure_ascii=False), flush=True)

    report = {"parsed": False, "downloaded": False, "decoded": False}
    try:
        videos = analyze(args.url, cancel, notify)
        report.update({"parsed": True, "video_count": len(videos),
                       "heights": [v.heights for v in videos]})
        if args.download:
            video = videos[0]
            height = args.height if args.height else (min(video.heights) if video.heights else None)
            video.title = "page_download_verification"
            path = download(video, height, args.output, cancel, notify)
            report.update({"downloaded": True, "height": height,
                           "file": str(path), "bytes": path.stat().st_size})
            report["media"] = inspect_local_media(path)
            actual_height = report["media"]["height"]
            report["quality_matches"] = height is None or (actual_height is not None and actual_height <= height + height % 2)
            decoded = subprocess.run([ffmpeg_path(), "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path), "-f", "null", "-"],
                                     capture_output=True, creationflags=CREATE_NO_WINDOW)
            report["decoded"] = decoded.returncode == 0
            if decoded.returncode:
                report["error"] = decoded.stderr.decode("utf-8", errors="replace")[-1200:]
    except KeyboardInterrupt:
        cancel.set()
        report["cancelled"] = True
    except Exception as exc:
        hint = explain_error(exc, "download" if report["parsed"] else "analyze")
        report.update({"error_code": hint.code, "error": hint.as_text(), "detail": hint.detail})
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "last_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["parsed"] and (not args.download or (report["decoded"] and report["quality_matches"])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
