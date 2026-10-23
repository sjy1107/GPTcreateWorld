"""Extraction, page discovery and MP4 processing, independent of the GUI."""
from __future__ import annotations

import copy
import http.cookiejar
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import imageio_ffmpeg
import requests
import yt_dlp
from yt_dlp.utils import std_headers
from .errors import AnalysisError, InvalidMediaError, explain_error, redact_detail
from .browser_cookies import load_cookie_file
from .resume import ResumeChanged, ResumeYoutubeDL, SafeYoutubeDL, download_workspace
from .site_compat import apply_download_compat, fallback_for, playback_url
from .format_policy import MP4_FORMAT_SORT, format_selector
from .network_retry import Cancelled, RetryController, RetryYoutubeDL, check_cancel
from .url_tools import validate_url
from .web_discovery import Candidate, discover_html
from .naming import filename_stem, publish_mp4, safe_stem
from .storage import StorageGuard, preflight_destination
from .result_notices import NoticeCapture, notices_for

Notify = Callable[[str, dict], None]
MAX_VIDEOS = 50
USER_AGENT = std_headers["User-Agent"]
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class MediaDownloadError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        hint = {
            403: "媒体服务器拒绝下载（HTTP 403），可能是链接已过期、登录状态不足或网络出口限制。",
            410: "媒体服务器返回 HTTP 410，该播放地址已不可用。",
            474: "视频页面已解析，但媒体服务器拒绝下载（HTTP 474）。请检查同一网络下的浏览器播放状态和自己的登录 Cookies。",
        }.get(status, f"媒体下载失败（HTTP {status}）。")
        super().__init__(hint + "\n" + detail)


@dataclass
class Network:
    proxy: str = ""
    cookie_file: str = ""
    rate_limit: int = 0  # Bytes per second; zero leaves the connection unrestricted.
    keep_progress: bool = False


def js_runtimes() -> dict:
    """Use an installed runtime; never fetch remote solver code automatically."""
    node = os.environ.get("YTDLP_NODE_PATH") or shutil.which("node")
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    if not node and bundled.is_file():
        node = str(bundled)
    runtimes = {"deno": {}}
    if node and Path(node).is_file():
        runtimes["node"] = {"path": str(node)}
    return runtimes


def ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


class Logger:
    def __init__(self, notify: Notify, cancel: threading.Event):
        self.notify, self.cancel = notify, cancel
        self.last_error = ""
        self.capture = NoticeCapture()

    def debug(self, message):
        check_cancel(self.cancel)
        self.capture.record(message)

    def warning(self, message):
        check_cancel(self.cancel)
        self.capture.record(message)
        if "ffprobe not found" in str(message):
            message = "未读取到完整媒体元数据，将继续使用 FFmpeg 生成 MP4。"
        self.notify("log", {"text": "提示：" + redact_detail(message)})

    def error(self, message):
        check_cancel(self.cancel)
        self.last_error = str(message)
        self.notify("log", {"text": redact_detail(self.last_error)})


def options(notify: Notify, cancel: threading.Event, referer: str = "", network: Network | None = None) -> dict:
    # Site extractors/transport clients can choose their own compatible UA.
    headers = {}
    if referer:
        headers["Referer"] = referer
    retry = RetryController(notify, cancel)
    opts = {
        "quiet": True, "no_warnings": False, "logger": Logger(notify, cancel),
        "socket_timeout": 15, "retries": 2, "fragment_retries": 2,
        "extractor_retries": 1, "http_headers": headers,
        "ffmpeg_location": ffmpeg_path(), "cachedir": False,
        "playlistend": MAX_VIDEOS, "ignoreerrors": True,
        "allow_unplayable_formats": False,
        "skip_unavailable_fragments": False,
        "js_runtimes": js_runtimes(),
        "format_sort": list(MP4_FORMAT_SORT),
        "retry_sleep_functions": retry.callbacks(), "_shiying_retry": retry,
    }
    if network:
        if network.rate_limit > 0:
            opts["ratelimit"] = network.rate_limit
        if network.proxy:
            opts["proxy"] = network.proxy
        if network.cookie_file:
            opts["cookiefile"] = network.cookie_file
    return opts


def flatten(info):
    if not info:
        return
    if info.get("_type") in ("playlist", "multi_video"):
        for entry in info.get("entries") or []:
            yield from flatten(entry)
    else:
        yield info


def video_formats(info: dict) -> list[dict]:
    formats = info.get("formats") or ([info] if info.get("url") else [])
    return [f for f in formats if f.get("vcodec") != "none" and not f.get("has_drm")
            and f.get("ext") not in ("mhtml", "jpg", "png", "vtt")
            and f.get("protocol") != "mhtml"]


def media_urls(info: dict) -> set[str]:
    # Different videos can share an audio track. Only a shared video resource
    # identifies duplicate video results.
    return {url for f in video_formats(info)
            for url in (f.get("url"), f.get("manifest_url")) if url}


@dataclass
class Video:
    key: str
    title: str
    duration: float | None
    heights: list[int]
    info: dict
    referer: str

    @property
    def notices(self):
        return notices_for(self.info)

    @classmethod
    def from_info(cls, info: dict, referer: str):
        formats = video_formats(info)
        if info.get("is_live") or info.get("live_status") == "is_live":
            raise ValueError("首版暂不支持正在直播的视频，请等待直播结束后再解析。")
        if info.get("has_drm") or (not formats and any(f.get("has_drm") and f.get("vcodec") != "none" for f in info.get("formats") or [])):
            raise ValueError("该视频受到 DRM 保护，当前版本无法保存为普通 MP4。")
        if not formats:
            raise ValueError("资源没有可下载的视频画面，可能只有声音、图片，或画面信息未加载。")
        heights = set()
        for fmt in formats:
            try:
                height = int(fmt.get("height"))
                if height > 0:
                    heights.add(height)
            except (TypeError, ValueError, OverflowError):
                pass
        try:
            duration = float(info.get("duration"))
            if not math.isfinite(duration) or duration < 0:
                duration = None
        except (TypeError, ValueError, OverflowError):
            duration = None
        return cls(uuid.uuid4().hex, str(info.get("title") or info.get("id") or "未命名视频"),
                   duration, sorted(heights, reverse=True), info, referer)


def page_candidates(url: str, cancel: threading.Event, network: Network | None = None) -> list[Candidate]:
    check_cancel(cancel)
    if re.search(r"\.(mp4|m3u8|mpd|webm|mov)(?:[?#]|$)", url, re.I):
        return []
    kwargs = {}
    if network:
        if network.proxy:
            kwargs["proxies"] = {"http": network.proxy, "https": network.proxy}
        if network.cookie_file:
            kwargs["cookies"] = load_cookie_file(network.cookie_file)
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True,
                      timeout=(10, 15), **kwargs) as response:
        response.raise_for_status()
        if "html" not in response.headers.get("Content-Type", "").lower():
            return []
        data = bytearray()
        for chunk in response.iter_content(65536):
            check_cancel(cancel)
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("网页超过 8 MB，已停止额外的标签扫描。")
        # Let BeautifulSoup inspect the declared HTML encoding when no HTTP charset exists.
        html = bytes(data).decode(response.encoding, errors="replace") if "charset=" in response.headers.get("Content-Type", "") else bytes(data)
        return discover_html(html, response.url)


class PageYoutubeDL(RetryYoutubeDL):
    """Remember the root extractor before transparent embeds replace its metadata."""
    root_extractor = ""

    def extract_info(self, url, *args, **kwargs):
        capture = getattr(self.params.get('logger'), 'capture', None)
        old_url = getattr(self, '_notice_url', '')
        old_context = capture.context if capture else None
        self._notice_url = url
        if capture:
            capture.context = None
        try:
            result = super().extract_info(url, *args, **kwargs)
            if capture and isinstance(result, dict) and result.get('_type') not in ('playlist', 'multi_video'):
                capture.apply(result)
            return result
        finally:
            self._notice_url = old_url
            if capture:
                capture.context = old_context

    def get_info_extractor(self, ie_key):
        extractor = super().get_info_extractor(ie_key)
        if not self.root_extractor:
            self.root_extractor = extractor.ie_key()
        capture = getattr(self.params.get('logger'), 'capture', None)
        if capture and getattr(self, '_notice_url', ''):
            try:
                identity = extractor.get_temp_id(self._notice_url)
            except Exception:
                identity = None
            capture.context = (extractor.ie_key().lower(), str(identity)) if identity else None
        return extractor

    def add_default_extra_info(self, ie_result, ie, url):
        if not self.root_extractor and ie is not None:
            self.root_extractor = ie.ie_key()
        result = super().add_default_extra_info(ie_result, ie, url)
        capture = getattr(self.params.get('logger'), 'capture', None)
        if capture:
            capture.apply(ie_result)
        return result


def missing_entries(info, depth=0):
    if not isinstance(info, dict) or depth > 6 or info.get('_type') not in ('playlist', 'multi_video'):
        return 0
    entries = info.get('entries')
    if not isinstance(entries, (list, tuple)):
        return 0
    return sum(1 if entry is None else missing_entries(entry, depth + 1) for entry in entries[:MAX_VIDEOS])


def generic_retry_allowed(errors):
    # A different parser can help an unsupported page, not missing permissions
    # or a refused connection. Do not obscure the primary actionable error.
    if not errors or any(explain_error(error, "analyze").code not in ("not_detected", "unknown") for error in errors):
        return False
    return any(re.search(r"unsupported url|unable to extract|no video formats|no video (?:could be )?found|could not find video", str(error), re.I)
               for error in errors)


def analyze(url: str, cancel: threading.Event, notify: Notify, network: Network | None = None) -> list[Video]:
    url = playback_url(validate_url(url))
    parts = urlsplit(url)
    if parts.hostname in ("t.me", "www.t.me"):
        if parts.path.startswith(("/c/", "/+", "/joinchat/")):
            raise ValueError("此 Telegram 链接需要账号或频道成员权限。首版仅支持可公开访问的消息网页链接。")
        normalized = re.sub(r"^/s/([^/]+/\d+)$", r"/\1", parts.path)
        url = urlunsplit((parts.scheme, parts.netloc, normalized, parts.query, ""))
    videos, seen, seen_ids = [], set(), set()
    root_generic = False
    omitted = 0
    errors = []

    def add(info):
        for entry in flatten(info):
            check_cancel(cancel)
            if len(videos) >= MAX_VIDEOS:
                break
            urls = media_urls(entry)
            extractor = str(entry.get("extractor_key") or "").lower()
            identity = (extractor, str(entry.get("id"))) if extractor not in ("", "generic", "html5mediaembed") and entry.get("id") else None
            if urls & seen or (identity is not None and identity in seen_ids):
                continue
            try:
                video = Video.from_info(entry, url)
            except ValueError as exc:
                errors.append(exc)
                notify("log", {"text": str(exc)})
                continue
            seen.update(urls)
            if identity is not None:
                seen_ids.add(identity)
            videos.append(video)
            notify("log", {"text": f"已识别：{video.title}"})

    main_options = options(notify, cancel, network=network)
    with PageYoutubeDL(main_options) as ydl:
        notify("log", {"text": "正在解析视频信息与可用清晰度…"})
        try:
            info = ydl.extract_info(url, download=False)
            omitted += missing_entries(info)
            add(info)
            if not videos and main_options["logger"].last_error:
                errors.append(RuntimeError(main_options["logger"].last_error))
        except Cancelled:
            raise
        except Exception as exc:
            check_cancel(cancel)
            errors.append(exc)
            notify("log", {"text": "主解析未完成，继续扫描网页视频标签。"})
        root_generic = ydl.root_extractor.lower() == "generic"

    check_cancel(cancel)
    fallback = fallback_for(url, errors) if not videos else None
    if fallback:
        notify("log", {"text": fallback.message})
        fallback_options = options(notify, cancel, url, network)
        fallback_options.update(ignoreerrors=False, noplaylist=True)
        try:
            with RetryYoutubeDL(fallback_options) as ydl:
                fallback.configure(ydl)
                info = ydl.extract_info(fallback.url, download=False)
            check_cancel(cancel)
            entries = list(flatten(info))
            if len(entries) != 1 or str(entries[0].get("id")) != fallback.expected_id:
                raise ValueError("兼容接口未返回原视频，已停止该尝试。请重新查找原视频。")
            add(entries[0])
        except Cancelled:
            raise
        except Exception as exc:
            check_cancel(cancel)
            # The player can reveal the actual restriction hidden by the
            # default API client's generic login requirement.
            errors.insert(0, exc)

    if not videos and not root_generic and generic_retry_allowed(errors):
        check_cancel(cancel)
        notify("log", {"text": "站点解析器未找到资源，正在用通用播放器规则补充查找…"})
        generic_options = options(notify, cancel, network=network)
        generic_options.update(ignoreerrors=False, noplaylist=True)
        try:
            with PageYoutubeDL(generic_options) as ydl:
                add(ydl.extract_info(url, download=False, force_generic_extractor=True))
            root_generic = True
        except Cancelled:
            raise
        except Exception as exc:
            check_cancel(cancel)
            errors.append(exc)

    # A dedicated site extractor already understands its playlist. Generic pages
    # additionally need tag scanning to discover resources the generic extractor missed.
    generic = root_generic or not videos or any(v.info.get("extractor_key", "Generic").lower() in ("generic", "html5mediaembed") for v in videos)
    if generic:
        notify("log", {"text": "正在补充查找延迟加载播放器、网页视频清单与媒体链接…"})
        check_cancel(cancel)
        try:
            candidates = page_candidates(url, cancel, network)
        except Cancelled:
            raise
        except Exception as exc:
            candidates = []
            errors.append(exc)
            notify("log", {"text": "额外的网页扫描未完成：" + str(exc)})
        for index, candidate in enumerate(candidates, 1):
            check_cancel(cancel)
            if len(videos) >= MAX_VIDEOS:
                break
            if set(candidate.urls).issubset(seen):
                continue
            existing = next((video for video in videos if media_urls(video.info).intersection(candidate.urls)), None)
            notify("log", {"text": f"检查网页资源 {index}/{len(candidates)}…"})
            grouped = []
            for source_index, source in enumerate(candidate.urls):
                check_cancel(cancel)
                if source in seen:
                    continue
                try:
                    opts = options(notify, cancel, url, network)
                    with RetryYoutubeDL(opts) as ydl:
                        entries = list(flatten(ydl.extract_info(source, download=False)))
                    if len(entries) > 1:
                        for entry in entries:
                            add(entry)
                    elif entries:
                        entry = entries[0]
                        # Check every alternative before combining it; a live
                        # or protected second source must not inherit VOD flags.
                        Video.from_info(entry, url)
                        entry["formats"] = copy.deepcopy([f for f in (entry.get("formats") or [entry]) if not f.get("has_drm")])
                        for fmt in entry["formats"]:
                            fmt["format_id"] = f"s{source_index}-{fmt.get('format_id', 'original')}"
                        grouped.append(entry)
                except Cancelled:
                    raise
                except Exception as exc:
                    check_cancel(cancel)
                    errors.append(exc)
                    notify("log", {"text": "该资源暂不可解析：" + str(exc)})
            if grouped:
                merged = copy.deepcopy(existing.info) if existing else grouped[0]
                current_formats = copy.deepcopy(existing.info.get("formats") or [existing.info]) if existing else []
                merged["formats"] = current_formats + [fmt for item in grouped for fmt in item["formats"]]
                if candidate.title:
                    merged["title"] = candidate.title
                if existing:
                    updated = Video.from_info(merged, url)
                    existing.info, existing.heights = updated.info, updated.heights
                    existing.title, existing.duration = updated.title, updated.duration
                    seen.update(media_urls(merged))
                    notify("log", {"text": f"已补充该视频的可用资源：{existing.title}"})
                else:
                    add(merged)
                seen.update(candidate.urls)
    check_cancel(cancel)
    if not videos:
        raise AnalysisError(errors) from None
    if len(videos) >= MAX_VIDEOS:
        notify("log", {"text": f"演示版每次最多解析 {MAX_VIDEOS} 个视频，较长列表请拆分网址。"})
    collection = []
    if omitted:
        collection.append(f'所属网页或列表有 {omitted} 个条目未能解析；当前视频是否完整需分别确认。')
    if errors:
        collection.append(f'本次有 {len(errors)} 次解析或补充检查未完成。这不等于遗漏了同样数量的视频，也不表示当前视频损坏。')
    if len(videos) >= MAX_VIDEOS:
        collection.append(f'本次达到 {MAX_VIDEOS} 个视频上限，未确认列表是否还有其他条目。')
    if collection:
        for video in videos:
            video.info['_shiying_collection_notice'] = '\n'.join(collection)
    return videos


def inspect_local_media(source: Path) -> dict:
    probe = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(source)],
                           capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
    metadata = probe.stderr.decode("utf-8", errors="replace")
    video_line = next((line for line in metadata.splitlines() if "Video:" in line), "")
    audio_line = next((line for line in metadata.splitlines() if "Audio:" in line), "")
    video_codec = re.search(r"Video:\s+([\w]+)", video_line)
    audio_codec = re.search(r"Audio:\s+([\w]+)", audio_line)
    pixels = re.search(r"\b(yuv[a-z0-9]*|yuva[a-z0-9]*|nv12|rgb[a-z0-9]*|bgr[a-z0-9]*|gbrp[a-z0-9]*)\b", video_line)
    color_range = re.search(r"\b(?:yuv[a-z0-9]*|nv12|gbrp[a-z0-9]*)\((pc|tv)(?:[,)]|\s)", video_line)
    size = re.search(r"\b(\d{2,6})x(\d{2,6})\b", video_line)
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", metadata)
    return {
        "video_codec": video_codec[1] if video_codec else None,
        "audio_codec": audio_codec[1] if audio_codec else None,
        "pixel_format": pixels[1] if pixels else None,
        "color_range": color_range[1] if color_range else "pc" if pixels and pixels[1].startswith("yuvj") else None,
        "width": int(size[1]) if size else None, "height": int(size[2]) if size else None,
        "duration": int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3]) if duration else None,
    }


def convert_mp4(source: Path, target: Path, cancel: threading.Event,
                notify: Notify, duration: float | None = None, *, storage=None):
    """Produce H.264/AAC MP4, including when the source already has an .mp4 suffix."""
    check_cancel(cancel)
    media = inspect_local_media(source)
    check_cancel(cancel)
    if not media["video_codec"]:
        raise InvalidMediaError("服务器返回的文件没有可用的视频画面，可能是提示网页或已损坏的数据。")
    duration = duration or media["duration"]
    compatible = (media["video_codec"] == "h264" and media["pixel_format"] == "yuv420p"
                  and media.get("color_range") != "pc" and media["audio_codec"] in (None, "aac"))
    codec_args = ["-c", "copy"] if compatible else [
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-vf", "scale=in_range=auto:out_range=tv,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
        "-pix_fmt", "yuv420p", "-color_range", "tv", "-c:a", "aac", "-b:a", "192k"]
    phase = "封装为 MP4" if compatible else "转码为 MP4"
    if storage:
        storage.before_processing([source])
    notify("progress", {"phase": phase, "percent": 0})
    command = [ffmpeg_path(), "-hide_banner", "-nostdin", "-y", "-xerror", "-i", str(source),
               "-map", "0:v:0", "-map", "0:a:0?", *codec_args, "-movflags", "+faststart",
               "-progress", "pipe:1", "-nostats", str(target)]
    # File-backed logs avoid blocking pipes. A polling loop can cancel even when
    # ffmpeg is still opening a source and has not emitted progress yet.
    with tempfile.TemporaryDirectory(prefix="mp4-progress-") as log_dir:
        progress_file = Path(log_dir) / "progress.txt"
        error_file = Path(log_dir) / "errors.txt"
        output = progress_file.open("wb")
        errors = error_file.open("wb")
        try:
            process = subprocess.Popen(command, stdout=output, stderr=errors, creationflags=CREATE_NO_WINDOW)
        except BaseException:
            output.close()
            errors.close()
            raise
        offset, pending = 0, b""
        try:
            while process.poll() is None:
                check_cancel(cancel)
                if storage:
                    storage.check(file=target)
                with progress_file.open("rb") as reader:
                    reader.seek(offset)
                    chunk = reader.read()
                    offset += len(chunk)
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if line.startswith(b"out_time_us=") and duration:
                        try:
                            percent = min(99.0, int(line.split(b"=", 1)[1]) / 1_000_000 / duration * 100)
                            notify("progress", {"phase": phase, "percent": max(0, percent)})
                        except ValueError:
                            pass
                cancel.wait(0.15)
            check_cancel(cancel)
            if process.returncode:
                detail = error_file.read_text(encoding="utf-8", errors="replace")[-2000:]
                if any(marker in detail.lower() for marker in ("invalid data found when processing input", "error while decoding", "moov atom not found")):
                    raise InvalidMediaError("下载的素材无法正常读取，请重新查找视频再试。\n" + detail)
                raise RuntimeError("MP4 转换失败：" + detail)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            output.close()
            errors.close()
    if not target.exists() or target.stat().st_size < 128:
        raise RuntimeError("未生成有效的 MP4 文件。")
    check_cancel(cancel)
    output_media = inspect_local_media(target)
    check_cancel(cancel)
    if (output_media["video_codec"] != "h264" or output_media["pixel_format"] != "yuv420p"
            or output_media.get("color_range") == "pc" or output_media["audio_codec"] not in (None, "aac")):
        raise RuntimeError("MP4 转换失败：输出没有达到兼容的 H.264 / AAC / 8 位格式，请保留错误详情以便排查。")
    return output_media


def refresh_video(video: Video, cancel: threading.Event, notify: Notify,
                  network: Network | None = None) -> Video:
    """Resolve the same logical item again, never fall back to the first video."""
    fresh = analyze(video.referer, cancel, notify, network)
    key = (str(video.info.get("extractor_key") or video.info.get("extractor") or "").lower(),
           str(video.info.get("id") or ""))
    if not key[1]:
        raise ValueError("无法确认原视频身份，请重新解析并选择视频。")
    matches = [v for v in fresh if (
        str(v.info.get("extractor_key") or v.info.get("extractor") or "").lower(),
        str(v.info.get("id") or "")) == key]
    if key[0] in ("generic", "html5mediaembed"):
        # Generic extractors can assign IDs by page position. For such pages,
        # also require a stable resource path; signed query strings may change.
        def resources(item):
            return {urlunsplit((*urlsplit(url)[:3], "", "")) for url in media_urls(item.info)}
        original = resources(video)
        matches = [v for v in matches if resources(v) & original]
    if len(matches) != 1:
        raise ValueError("网页中的视频已变化或无法唯一定位原视频，请重新解析并确认。")
    refreshed = matches[0]
    refreshed.key, refreshed.title = video.key, video.title
    return refreshed


def download(video: Video, height: int | None, folder: Path,
             cancel: threading.Event, notify: Notify, network: Network | None = None,
             *, filename: str = "", naming: str = "title_quality") -> Path:
    def attempt(item):
        for number in range(2):
            try:
                return _download_once(item, height, folder, cancel, notify, network, filename=filename, naming=naming)
            except ResumeChanged:
                check_cancel(cancel)
                if number:
                    raise RuntimeError("服务器上的文件多次变化，已停止续传。请稍后重新查找视频再试。") from None
                notify("log", {"text": "传输中发现文件变化，已丢弃不匹配的进度，重新下载一次…"})
    try:
        return attempt(video)
    except MediaDownloadError as exc:
        if exc.status not in (403, 410) or re.search(
                r"\.(mp4|m3u8|mpd|webm|mov)(?:[?#]|$)", video.referer, re.I):
            raise
        check_cancel(cancel)
        notify("log", {"text": f"媒体地址返回 HTTP {exc.status}，正在重新解析原网页并重试一次…"})
        notify("progress", {"phase": "刷新视频地址", "percent": None})
        try:
            refreshed = refresh_video(video, cancel, notify, network)
        except Cancelled:
            raise
        except Exception as refresh_error:
            check_cancel(cancel)
            raise RuntimeError("下载地址刷新失败，请重新解析或检查登录和网络。\n" + str(refresh_error)) from refresh_error
        if height is not None and height not in refreshed.heights:
            raise ValueError("刷新后所选清晰度已不可用，请重新解析并选择清晰度。")
        try:
            result = attempt(refreshed)
        except MediaDownloadError as retry_error:
            raise RuntimeError("已重新解析并重试一次，媒体服务器仍拒绝下载。请检查自己的登录状态、网页播放状态或网络。\n" + str(retry_error)) from retry_error
        video.info = refreshed.info
        video.duration = refreshed.duration
        video.heights = refreshed.heights
        notify("log", {"text": "视频地址刷新成功，下载已完成。"})
        return result


def _download_once(video: Video, height: int | None, folder: Path,
             cancel: threading.Event, notify: Notify, network: Network | None = None,
             *, filename: str = "", naming: str = "title_quality") -> Path:
    check_cancel(cancel)
    folder = folder.expanduser().resolve()
    if height is not None and height not in video.heights:
        raise ValueError("所选清晰度不在解析结果中，请重新解析。")
    keep_progress = bool(network and network.keep_progress)
    resuming = False
    from .transfer import estimate_video_bytes
    stem = filename_stem(video.title, height, filename=filename, naming=naming)
    volume = preflight_destination(folder, estimated_bytes=estimate_video_bytes(video, height), stems=[stem])
    storage = StorageGuard(folder, notify, volume)
    storage.check(force=True)

    def hook(data):
        if data.get('status') not in ('downloading', 'finished'):
            return
        check_cancel(cancel)
        storage.check(file_bytes=data.get('total_bytes') or data.get('downloaded_bytes'))
        if data["status"] == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            notify("progress", {
                "phase": "下载", "percent": min(100.0, data.get("downloaded_bytes", 0) / total * 100) if total else None,
                "speed": data.get("speed"), "eta": data.get("eta"),
                "resumed": resuming,
            })
        elif data["status"] == "finished":
            notify("progress", {"phase": "下载完成，准备合并", "percent": None})

    def post_hook(data):
        if data.get('status') not in ('started', 'processing', 'finished'):
            return
        check_cancel(cancel)
        if data.get('status') == 'started':
            storage.before_processing(list(work.glob('source.*')))
        else:
            storage.check(force=True)
        notify("progress", {"phase": "正在合并音视频", "percent": None})

    # Publish only a successful conversion. With retention enabled, privately
    # keep media and URL-free validation metadata on the destination drive.
    with download_workspace(video, height, folder, notify, keep_progress) as workspace:
        work = workspace.path
        opts = options(notify, cancel, video.referer, network)
        opts.update({"format": format_selector(height), "noplaylist": True, "ignoreerrors": False,
                     "outtmpl": str(work / "source.%(ext)s"), "merge_output_format": "mkv",
                     "progress_hooks": [hook], "postprocessor_hooks": [post_hook],
                     "overwrites": not keep_progress, "continuedl": keep_progress})
        check_cancel(cancel)
        info = copy.deepcopy(video.info)
        apply_download_compat(video.referer, opts, info, notify)
        downloader = ResumeYoutubeDL(opts, workspace) if keep_progress else SafeYoutubeDL(opts)
        source = None
        with downloader as ydl:
            # Re-run format selection against the complete format list, preserving
            # referers and signed media URLs obtained during the analysis.
            for key in ("requested_downloads", "requested_formats", "__files_to_move", "_filename", "filepath"):
                info.pop(key, None)
            try:
                if keep_progress:
                    preview = ydl.process_ie_result(copy.deepcopy(info), download=False)
                    workspace.prepare(preview)
                    source = workspace.ready_source()
                    if not source:
                        workspace.verify(ydl, lambda: check_cancel(cancel))
                        resuming = workspace.resuming
                result = ydl.process_ie_result(info, download=True) if not source else None
            except Exception as exc:
                check_cancel(cancel)
                detail = str(exc)
                status = re.search(r"HTTP Error (\d{3})", detail)
                if status:
                    raise MediaDownloadError(int(status[1]), detail) from exc
                raise
        check_cancel(cancel)
        source = source or (Path(result["filepath"]) if result and result.get("filepath") else None)
        if not source or not source.is_file():
            files = [p for p in work.glob("source.*") if p.is_file() and p.suffix not in (".part", ".ytdl") and p.name.count(".") == 1]
            if len(files) != 1:
                raise RuntimeError("下载未生成完整媒体文件，请重新解析网址后重试。")
            source = files[0]
        if keep_progress:
            workspace.save_ready(source)
        notify("progress", {"phase": "转为 MP4", "percent": 0})
        converted = work / "converted.mp4"
        try:
            media = convert_mp4(source, converted, cancel, notify, video.duration, storage=storage)
        except InvalidMediaError:
            if keep_progress:
                workspace.discard = True
            raise
        check_cancel(cancel)
        storage.check(force=True, file=converted)
        actual_height = media.get("height") if media else height
        stem = filename_stem(video.title, actual_height, filename=filename, naming=naming)
        target = publish_mp4(converted, folder, stem)
        if keep_progress:
            workspace.success = True
    notify("progress", {"phase": "完成", "percent": 100})
    return target
