"""Small, bounded adaptations for playback pages; no account or browser access."""
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from yt_dlp.extractor.tiktok import DouyinIE
from yt_dlp.utils import ExtractorError


@dataclass(frozen=True)
class Fallback:
    site: str
    url: str
    expected_id: str
    message: str
    extractor: type | None = None

    def configure(self, ydl):
        if self.extractor:
            ydl.add_info_extractor(self.extractor(ydl))


class SiteAdapter:
    key = ""
    hosts = ()
    subdomains = False

    def matches(self, url):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (parts.scheme in ("http", "https") and parts.port is None
                and not parts.username and not parts.password
                and any(host == item or (self.subdomains and host.endswith("." + item))
                        for item in self.hosts))

    def normalize(self, url):
        return url

    def fallback(self, url, errors):
        return None

    def prepare_download(self, opts, info, notify):
        pass


class DouyinAdapter(SiteAdapter):
    key, hosts = "douyin", ("douyin.com", "www.douyin.com")

    def normalize(self, url):
        parts = urlsplit(url)
        ids = parse_qs(parts.query).get("modal_id", [])
        if (parts.path in ("", "/") or parts.path.startswith("/user/")) and len(ids) == 1 and re.fullmatch(r"\d{10,25}", ids[0]):
            return f"https://www.douyin.com/video/{ids[0]}"
        return url

    def fallback(self, url, errors):
        match = re.fullmatch(r"/video/(\d{10,25})/?", urlsplit(url).path)
        if match and any("fresh cookies" in str(error).lower() for error in errors):
            return Fallback(self.key, url, match[1],
                            "正在更新抖音访问记录，再尝试一次公开视频接口…", DouyinPublicIE)


class VimeoAdapter(SiteAdapter):
    key, hosts = "vimeo", ("vimeo.com", "www.vimeo.com")

    def fallback(self, url, errors):
        # Retain access parameters; private/password and unlisted path URLs
        # are not rewritten. The normal upstream extractor is always tried first.
        parts = urlsplit(url)
        match = re.fullmatch(r"/(\d+)/?", parts.path)
        if match and any("web client only works when logged-in" in str(error).lower() for error in errors):
            return Fallback(self.key, f"https://player.vimeo.com/video/{match[1]}" +
                            (f"?{parts.query}" if parts.query else ""), match[1],
                            "正在尝试同一视频的 Vimeo 公开播放器；密码和观看权限仍由网站确认…")


class BilibiliPlayerAdapter(SiteAdapter):
    key, hosts = "bilibili-player", ("player.bilibili.com",)

    def normalize(self, url):
        parts = urlsplit(url)
        if parts.path != "/player.html":
            return url
        query = parse_qs(parts.query)
        bvid = query.get("bvid", [])
        pages = query.get("page", ["1"])
        # New player links use bvid, while the bundled upstream player matcher
        # only accepts aid. Preserve the explicitly selected part; a cid-only
        # selection cannot reliably be converted to a page number locally.
        if (len(bvid) != 1 or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid[0])
                or len(pages) != 1 or not re.fullmatch(r"[1-9]\d{0,3}", pages[0])
                or (query.get("cid") and "page" not in query)):
            return url
        return f"https://www.bilibili.com/video/{bvid[0]}/?p={int(pages[0])}"


class PornhubAdapter(SiteAdapter):
    key, hosts, subdomains = "pornhub", ("pornhub.com",), True

    def prepare_download(self, opts, info, notify):
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget(client="safari")
        # Keep extractor-provided Origin/Referer and let curl-cffi select the UA.
        opts["http_headers"].pop("User-Agent", None)
        for item in [info, *(info.get("formats") or [])]:
            headers = item.get("http_headers")
            if isinstance(headers, dict):
                for name in list(headers):
                    if name.lower() == "user-agent":
                        headers.pop(name)
        notify("log", {"text": "使用站点媒体服务器兼容模式下载…"})


ADAPTERS = (DouyinAdapter(), VimeoAdapter(), BilibiliPlayerAdapter(), PornhubAdapter())


def adapter_for(url):
    try:
        return next((adapter for adapter in ADAPTERS if adapter.matches(url)), None)
    except ValueError:
        return None


def playback_url(url):
    adapter = adapter_for(url)
    return adapter.normalize(url) if adapter else url


def fallback_for(url, errors):
    adapter = adapter_for(url)
    return adapter.fallback(url, errors) if adapter else None


def apply_download_compat(url, opts, info, notify):
    adapter = adapter_for(url)
    if adapter:
        adapter.prepare_download(opts, info, notify)


def site_fallback(url, errors):
    """Compatibility facade for existing integrations/tests."""
    plan = fallback_for(url, errors)
    return (plan.site, plan.url, plan.expected_id) if plan else None


def vimeo_player_fallback(url, errors):
    plan = fallback_for(url, errors)
    return (plan.url, plan.expected_id) if plan and plan.site == "vimeo" else None


class DouyinPublicIE(DouyinIE):
    """Bounded fallback based on yt-dlp PR #17826 (Unlicense).

    https://github.com/yt-dlp/yt-dlp/pull/17826
    Uses only the normal API and its access records. Does not read browser
    profiles, solve interactive challenges, or provide account permissions.
    """
    @classmethod
    def ie_key(cls):
        # Replacing the existing key retains its position before GenericIE and
        # keeps the same video identity for history/refresh/duplicate matching.
        return "Douyin"

    def _real_extract(self, url):
        video_id = self._match_id(url)
        cookies = self._get_cookies(self._WEBPAGE_HOST)
        if not cookies.get("passport_csrf_token"):
            self._set_cookie(".douyin.com", "passport_csrf_token", uuid.uuid4().hex)
        if not cookies.get("ttwid"):
            self._request_webpage(self._WEBPAGE_HOST + "ttwid/register", video_id,
                                  data=json.dumps({"aid": 6383, "service": "www.douyin.com"}).encode(),
                                  headers={"Content-Type": "application/json"})
        cookies = self._get_cookies(self._WEBPAGE_HOST)
        uifid = next((cookie.value for name, cookie in cookies.items()
                      if name.lower().replace("_", "") in ("uifid", "uifidtemp")), None)
        if not uifid:
            response = self._request_webpage(self._WEBPAGE_HOST, video_id)
            uifid = response.headers.get("x-tt-trace-host")
        if not uifid:
            raise ExtractorError("抖音没有返回可用的访问记录，请在浏览器确认能播放后再导入当前网站 Cookies。", expected=True)
        query = {"aweme_id": video_id, "uifid": uifid, "timestamp": str(int(time.time()))}
        # Protocol constant from the upstream fix; this is not an account key.
        payload = f'{uifid}_{query["timestamp"]}_A96D855A08C0A9707F8BEF0D9A527E4E_{urlencode(query)}'
        query["x-secsdk-web-signature"] = hashlib.md5(payload.encode()).hexdigest()
        response = self._download_json(self._WEBPAGE_HOST + "aweme/v1/web/aweme/detail/", video_id, query=query)
        detail = response.get("aweme_detail") if isinstance(response, dict) else None
        if not isinstance(detail, dict):
            raise ExtractorError("Fresh cookies or browser verification are needed; Douyin returned no video information", expected=True)
        return self._parse_aweme_video_app(detail)
