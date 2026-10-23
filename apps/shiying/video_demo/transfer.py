"""Small offline helpers for batch input and approximate storage checks."""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass

import yt_dlp

from .core import format_selector, validate_url
from .format_policy import MP4_FORMAT_SORT

MAX_LINKS = 20
SPACE_MARGIN = 64 * 1024 * 1024


def shared_urls(text: str) -> list[str]:
    """Extract explicit HTTP(S) links without fetching or expanding them."""
    text = text.strip()
    if not text:
        raise ValueError("请先复制视频网址或包含网址的分享文字。")
    if len(text) > 256 * 1024:
        raise ValueError("粘贴的内容过长，请只保留视频网址或简短分享文字。")
    # A bare URL is authoritative: preserve query punctuation and signatures.
    pattern = r'''https?://[^\s<>"'“”‘’「」『』【】《》（），。！？；、]+'''
    if re.fullmatch(pattern, text, re.I):
        return [validate_url(text)]
    urls = []
    for match in re.finditer(pattern, text, re.I):
        candidate = match[0]
        # Remove a surrounding Markdown/parenthesis delimiter, without
        # stripping meaningful punctuation from signed URL query strings.
        if match.start() and text[match.start() - 1] in "([{":
            closing = {"(": ")", "[": "]", "{": "}"}[text[match.start() - 1]]
            opening = text[match.start() - 1]
            if candidate.endswith(closing) and candidate.count(closing) > candidate.count(opening):
                candidate = candidate[:-1]
        if "?" not in candidate and "#" not in candidate:
            candidate = candidate.rstrip(".,;!")
        try:
            url = validate_url(candidate)
        except ValueError:
            raise ValueError("分享文字里有无效网址，请检查链接是否完整；网址不能包含账号密码。") from None
        if url not in urls:
            urls.append(url)
        if len(urls) > MAX_LINKS:
            raise ValueError(f"一次最多添加 {MAX_LINKS} 个不同链接，请拆成几批。")
    if not urls:
        raise ValueError("没有找到 http:// 或 https:// 网址，请复制包含完整链接的分享文字。")
    return urls


def parse_urls(text: str) -> list[str]:
    """One complete URL per line, all validated before starting any work."""
    urls = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            if any(char.isspace() for char in line.strip()):
                raise ValueError
            url = validate_url(line)
        except ValueError:
            raise ValueError(f"第 {number} 行不是完整网址，请每行只放一个 http:// 或 https:// 链接。") from None
        if url not in urls:
            urls.append(url)
        if len(urls) > MAX_LINKS:
            raise ValueError(f"一次最多添加 {MAX_LINKS} 个不同链接，请拆成几批。")
    if not urls:
        raise ValueError("请先粘贴至少一个视频网页链接，每行一个。")
    return urls


def bytes_label(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024


def _format_bytes(fmt: dict, duration: float | None) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return math.ceil(value)
    bitrate = fmt.get("tbr")
    if isinstance(bitrate, (int, float)) and bitrate > 0 and duration and duration > 0:
        value = bitrate * 1000 / 8 * duration
        if math.isfinite(value):
            return math.ceil(value)
    return None


def estimate_video_bytes(video, height: int | None, *, ydl=None) -> int | None:
    """Use the same local format selector as downloading; never contact a site."""
    if ydl is None:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "cachedir": False, "check_formats": False,
                              "format_sort": list(MP4_FORMAT_SORT)}) as shared:
            return estimate_video_bytes(video, height, ydl=shared)
    formats = copy.deepcopy(video.info.get("formats") or ([video.info] if video.info.get("url") else []))
    formats = [fmt for fmt in formats if not fmt.get("has_drm") and fmt.get("ext") != "mhtml"]
    if not formats:
        return None
    try:
        # Re-sort older parsed results too, keeping estimates aligned with download.
        ydl.sort_formats({"formats": formats, "_format_sort_fields": video.info.get("_format_sort_fields")})
        selector = ydl.build_format_selector(format_selector(height))
        selected = list(selector({
                "formats": formats,
                "has_merged_format": any("none" not in (f.get("acodec"), f.get("vcodec")) for f in formats),
                "incomplete_formats": all(f.get("vcodec") == "none" for f in formats)
                or all(f.get("acodec") == "none" for f in formats),
        }))
        if len(selected) != 1:
            return None
        tracks = selected[0].get("requested_formats") or selected
        sizes = [_format_bytes(track, video.duration) for track in tracks]
        return sum(sizes) if all(size is not None for size in sizes) else None
    except (KeyError, TypeError, ValueError, yt_dlp.utils.DownloadError):
        # Sites can omit metadata. Unknown size must never be displayed as zero.
        return None


@dataclass(frozen=True)
class SpaceEstimate:
    source_bytes: int
    recommended_bytes: int
    unknown: int
    count: int


def estimate_sizes(items) -> dict:
    """Row -> source bytes, sharing one offline selector for this batch."""
    if not items:
        return {}
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "cachedir": False, "check_formats": False,
                          "format_sort": list(MP4_FORMAT_SORT)}) as ydl:
        estimates = {}
        sizes = {}
        for row, video, quality in items:
            key = (id(video.info), quality)
            if key not in estimates:
                estimates[key] = estimate_video_bytes(video, quality, ydl=ydl)
            sizes[row] = estimates[key]
    return sizes


def estimate_space(items, *, sizes=None) -> SpaceEstimate:
    sizes = list((estimate_sizes(items) if sizes is None else sizes).values())
    known = [size for size in sizes if size is not None]
    # Finished files plus the largest in-flight download, merge and conversion.
    # The factor is a planning cushion, never a promise about re-encoded size.
    suggested = sum(known) + 2 * max(known, default=0) + SPACE_MARGIN
    return SpaceEstimate(sum(known), suggested, len(sizes) - len(known), len(sizes))
