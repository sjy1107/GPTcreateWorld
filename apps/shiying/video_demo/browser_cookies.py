"""Explicit local browser-session import, scoped before use by a task."""
from __future__ import annotations

import copy
import io
import json
import math
import os
import re
from dataclasses import dataclass, field
from http.cookiejar import Cookie
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser


SITE_GROUPS = (
    ("instagram.com",), ("x.com", "twitter.com"),
    ("bilibili.com", "b23.tv"), ("douyin.com", "iesdouyin.com"),
    ("pornhub.com",), ("xvideos.com",),
    ("youtube.com", "youtu.be"), ("t.me",),
)
BROWSERS = {"firefox", "edge", "chrome", "brave"}


def browser_root(browser: str) -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    return {"edge": local / "Microsoft/Edge/User Data", "chrome": local / "Google/Chrome/User Data",
            "brave": local / "BraveSoftware/Brave-Browser/User Data", "firefox": roaming / "Mozilla/Firefox/Profiles"}[browser]


def discover_profiles(browser: str) -> list[Path]:
    """Inspect directory names/file existence only, never cookie values."""
    root = browser_root(browser)
    try:
        profiles = [path for path in root.iterdir() if path.is_dir() and (
            (path / "cookies.sqlite").is_file() if browser == "firefox"
            else (path / "Network/Cookies").is_file() or (path / "Cookies").is_file())]
        return sorted(profiles, key=lambda path: (path.name != "Default", path.name.lower()))
    except OSError:
        return []


def normalize_profile(browser: str, profile: str) -> str | None:
    value = os.path.expandvars(profile.strip().strip('"')).strip()
    if not value:
        profiles = discover_profiles(browser)
        if len(profiles) > 1:
            raise BrowserCookieError("profile", "检测到多个浏览器用户，请先选择实际登录过网站的那个用户。")
        return str(profiles[0]) if profiles else None
    path = Path(value).expanduser()
    if path.name.lower() in ("cookies", "cookies.sqlite"):
        path = path.parent
        if browser != "firefox" and path.name.lower() == "network":
            path = path.parent
        return str(path)
    if browser != "firefox" and path.name.lower() == "network":
        return str(path.parent)
    if path.is_dir() and browser != "firefox" and not (path / "Network/Cookies").is_file() and not (path / "Cookies").is_file():
        candidates = [child for child in path.iterdir() if child.is_dir() and ((child / "Network/Cookies").is_file() or (child / "Cookies").is_file())]
        if len(candidates) > 1:
            raise BrowserCookieError("profile", "选择的是包含多个用户的总目录。请选其中实际使用的 Default 或 Profile 文件夹。")
        if len(candidates) == 1:
            return str(candidates[0])
    # A bare Chromium profile name is meaningful to yt-dlp; keep it as a name.
    return str(path) if path.is_absolute() or path.is_dir() else value


def inspect_cookie_file(path: str | Path, url: str | None = None):
    path = Path(path)
    with path.open("rb") as source:
        data = source.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("登录文件过大，请仅导出当前网站的 Cookies。")
    if data.startswith(b"SQLite format 3"):
        raise ValueError("这是浏览器内部的 Cookies 数据库，不能当作文本导入。请使用“读取浏览器”，或先导出当前网站的 Cookies .txt 文件。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("这不是可导入的 Cookies 文本。请按指引导出 Netscape / cookies.txt 格式文件。") from None
    return inspect_cookie_text(text, url)


def load_cookie_file(path: str | Path, url: str | None = None) -> YoutubeDLCookieJar:
    return inspect_cookie_file(path, url).jar


def load_cookie_text(text: str, url: str | None = None) -> YoutubeDLCookieJar:
    return inspect_cookie_text(text, url).jar


def _json_cookie_jar(text):
    """Accept browser-extension records, never infer domains from name/value maps."""
    message = "JSON 不是支持的 Cookies 导出列表，请导出含 domain、name、value 的记录，或改选 Netscape 格式；原登录设置仍保留。"
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("JSON 内容不完整或格式有误，请重新复制或导出 Cookies；不会显示原始登录内容。") from None
    records = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(records, list) or len(records) > 20000:
        raise ValueError(message)
    jar = YoutubeDLCookieJar()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError(message)
        domain, name, value, path = (item.get(key, default) for key, default in
                                     (("domain", None), ("name", None), ("value", None), ("path", "/")))
        if (any(not isinstance(field, str) for field in (domain, name, value, path))
                or not name or not path.startswith("/")
                or any(ord(char) < 32 or ord(char) == 127 for field in (domain, name, value, path) for char in field)
                or re.search(r'[()<>@,;:\\"/\[\]?={}\s]', name)):
            raise ValueError(message)
        leading_dot = domain.startswith(".")
        try:
            domain = domain.removeprefix(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError(message) from None
        if not re.fullmatch(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*", domain):
            raise ValueError(message)
        for flag in ("secure", "httpOnly", "hostOnly", "session", "partitioned"):
            if flag in item and not isinstance(item[flag], bool):
                raise ValueError(message)
        if item.get("partitioned") or item.get("partitionKey"):
            raise ValueError("JSON 中包含分区 Cookies，当前版本无法可靠使用这类记录；请尝试直接读取浏览器，或只导出普通的网站 Cookies。")
        expiration = item.get("expirationDate", item.get("expires"))
        if expiration is not None and (type(expiration) not in (int, float) or expiration < -1
                                       or expiration > 253402300799 or not math.isfinite(expiration)):
            raise ValueError(message)
        if item.get("session") is False and expiration is None:
            raise ValueError(message)
        expires = None if item.get("session") or expiration is None or expiration == -1 else int(expiration)
        host_only = item.get("hostOnly", not leading_dot)
        domain = domain.lstrip(".") if host_only else "." + domain.lstrip(".")
        rest = {"HttpOnly": None} if item.get("httpOnly") else {}
        jar.set_cookie(Cookie(0, name, value, None, False, domain, not host_only, domain.startswith("."),
                              path, True, item.get("secure", False), expires, expires is None, None, None, rest))
    return jar


@dataclass
class CookieInspection:
    jar: YoutubeDLCookieJar = field(repr=False)
    domains: tuple[str, ...] | None
    expired: int
    unrelated: int
    format: str

    def summary(self):
        site = " / ".join(self.domains) if self.domains else "导出文件中的网站"
        text = f"当前网站：{site}。可用 {len(self.jar)} 条 Cookies；忽略已过期 {self.expired} 条、其他网站 {self.unrelated} 条。"
        if self.domains:
            marker = BrowserSession("import", self.domains, tuple(self.jar)).login_marker
            text += ("\n未找到常见登录标记。请确认已在正确的浏览器用户中登录，再重新导出。" if marker is False else
                     "\n已找到常见登录标记，但仍需网站确认会话及观看权限。" if marker is True else
                     "\n这个网站暂不判断登录标记，是否能访问仍由网站确认。")
        return text


def inspect_cookie_jar(jar, url=None, *, format="本机会话"):
    roots = site_scope(url) if url else None
    matched, expired, unrelated = 0, 0, 0
    clean = YoutubeDLCookieJar()
    for cookie in jar:
        if roots and not domain_in_scope(cookie.domain, roots):
            unrelated += 1
        else:
            matched += 1
            if cookie.is_expired():
                expired += 1
            else:
                clean.set_cookie(copy.deepcopy(cookie))
    if not len(clean):
        if matched and expired:
            raise ValueError("当前网站的 Cookies 已全部过期。请在浏览器重新登录并确认视频能播放，然后重新导出。")
        if roots and unrelated:
            raise ValueError(f"没有当前网站（{' / '.join(roots)}）的 Cookies。请切回目标视频页面，只导出当前网站；不要停留在扩展商店或其他网站。")
        raise ValueError("导出内容中没有可用的 Cookies。请在已登录的视频页面重新导出。")
    return CookieInspection(clean, roots, expired, unrelated, format)


def inspect_cookie_text(text: str, url: str | None = None) -> CookieInspection:
    """The same validation for an exported file or an explicitly pasted export."""
    if len(text.encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("Cookies 内容过大，请仅导出当前网站。")
    # Clipboard exports and Windows files commonly use CRLF. The upstream jar
    # strips LF only from StringIO, so normalize before any parsing or values
    # can accidentally retain a CR and become invalid HTTP header content.
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    try:
        if text.lstrip().startswith(("{", "[")):
            return inspect_cookie_jar(_json_cookie_jar(text), url, format="JSON")
        if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
            raise ValueError("没有找到 Netscape 格式的 Cookies。请在导出扩展中选 Netscape，再点 Copy（复制）或 Export（导出）；不要复制网址或开发者工具里的表格。")
        for line in text.splitlines():
            if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            columns = line.removeprefix("#HttpOnly_").split("\t")
            if len(columns) != 7 or columns[1] not in ("TRUE", "FALSE") or columns[3] not in ("TRUE", "FALSE") or (
                    columns[4] and not columns[4].isdigit()) or (columns[1] == "TRUE") != columns[0].startswith("."):
                raise ValueError("Cookies 文本中有不完整或不符合 Netscape 格式的行，请重新导出；不会显示包含登录凭据的原始行。")
            if any(ord(char) < 32 or ord(char) == 127 for field in columns for char in field):
                raise ValueError("Cookies 文本含有不能用于网络请求的控制字符，请重新导出；不会显示原始登录内容。")
        jar = YoutubeDLCookieJar()
        jar.load(filename=io.StringIO(text))
    except (UnicodeError, OSError) as exc:
        raise ValueError("这不是可导入的 Cookies 文本。请按指引导出 Netscape / cookies.txt 格式文件。") from None
    return inspect_cookie_jar(jar, url, format="Netscape")


def domain_in_scope(domain: str, roots: tuple[str, ...]) -> bool:
    domain = domain.lower().lstrip(".").rstrip(".")
    return any(domain == root or domain.endswith("." + root) for root in roots)


def site_scope(url: str) -> tuple[str, ...]:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请先粘贴完整的视频网页网址，再读取该网站的登录状态。")
    host = parsed.hostname.lower().rstrip(".")
    for roots in SITE_GROUPS:
        if domain_in_scope(host, roots):
            return roots
    # Avoid guessing public suffixes. Unknown sites use this host (without www).
    return (host.removeprefix("www."),)


class BrowserCookieError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = "browser_cookie_" + code


@dataclass
class BrowserSession:
    browser: str
    domains: tuple[str, ...]
    cookies: tuple[Cookie, ...] = field(repr=False)
    partial: bool = False

    @property
    def login_marker(self) -> bool | None:
        names = {"instagram.com": "sessionid", "x.com": "auth_token", "bilibili.com": "SESSDATA"}
        key = next((names[domain] for domain in self.domains if domain in names), None)
        return any(cookie.name == key and bool(cookie.value) and not cookie.is_expired() for cookie in self.cookies) if key else None

    def supports(self, url: str) -> bool:
        try:
            return site_scope(url) == self.domains
        except ValueError:
            return False

    def write_task_file(self, path: Path):
        jar = YoutubeDLCookieJar(str(path))
        for cookie in self.cookies:
            if domain_in_scope(cookie.domain, self.domains) and not cookie.is_expired():
                jar.set_cookie(copy.deepcopy(cookie))
        if not len(jar):
            raise BrowserCookieError("expired", "已读取的当前网站 Cookies 已过期，请重新读取。")
        # yt-dlp represents session cookies as expiry=0 in Netscape files.
        # Work on copies because its save() mutates that field.
        jar.save()


class _CookieLogger:
    def __init__(self, check):
        self.check = check
        self.decryption_failed = False

    def debug(self, message, **kwargs):
        self.check()

    info = debug

    def warning(self, message, **kwargs):
        self.check()
        if any(word in str(message).lower() for word in ("decrypt", "dpapi", "v20", "app-bound")):
            self.decryption_failed = True

    error = warning


def read_browser_session(browser: str, profile: str, url: str, check: Callable[[], None]) -> BrowserSession:
    roots = site_scope(url)
    if browser not in BROWSERS:
        raise ValueError("请选择支持的浏览器。")
    check()
    logger = _CookieLogger(check)
    try:
        # Upstream reads a whole local profile. Discard unrelated sites before
        # returning a session; never log cookie names/values or save the full jar.
        jar = extract_cookies_from_browser(browser, normalize_profile(browser, profile), logger=logger)
    except Exception as exc:
        check()  # Preserve cancellation rather than turning it into an error.
        if isinstance(exc, BrowserCookieError):
            raise
        text = str(exc).lower()
        if any(word in text for word in ("decrypt", "dpapi", "app-bound", "v20")):
            raise BrowserCookieError("encrypted", "浏览器 Cookies 无法解密，可能受 Windows 浏览器加密保护。") from None
        if isinstance(exc, FileNotFoundError):
            raise BrowserCookieError("missing", "没有找到该浏览器的 Cookies 资料，请检查浏览器及用户资料位置。") from None
        if isinstance(exc, PermissionError) or any(word in text for word in ("could not copy", "locked", "permission denied", "access is denied")):
            raise BrowserCookieError("locked", f"{browser.title()} 的 Cookies 数据库正在被占用，或当前 Windows 用户无法读取。关闭浏览器窗口后仍可能有后台进程。") from None
        raise BrowserCookieError("read", "无法读取浏览器资料；可能是资料格式变化、组件或文件访问问题。") from None
    check()
    scoped = tuple(copy.deepcopy(cookie) for cookie in jar
                   if domain_in_scope(cookie.domain, roots) and not cookie.is_expired())
    if not scoped:
        if logger.decryption_failed:
            raise BrowserCookieError("encrypted", "当前网站没有可用的 Cookies，且浏览器报告了 Cookie 解密失败。")
        raise BrowserCookieError("empty", "该浏览器资料中没有当前网站尚未过期的 Cookies，请先在该资料中登录并打开视频。")
    return BrowserSession(browser, roots, scoped, logger.decryption_failed)
