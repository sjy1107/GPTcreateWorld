"""Turn technical failures into concise, actionable desktop messages."""
from __future__ import annotations

import errno
import re
from http import HTTPStatus
from dataclasses import dataclass, replace
from urllib.parse import urlsplit


class InvalidMediaError(RuntimeError):
    """The server returned data that contains no usable video stream."""


def redact_detail(value: object) -> str:
    text = str(value)
    def safe_url(match):
        try:
            parts = urlsplit(match[0])
            return f"{parts.scheme}://{parts.hostname or '未知主机'}/…"
        except ValueError:
            return "[网址]"
    text = re.sub(r"https?://[^\s<>\"']+", safe_url, text)
    text = re.sub(r"(?im)\b(cookie|authorization|proxy-authorization)\s*:\s*[^\r\n]+",
                  r"\1: [已隐藏]", text)
    text = re.sub(r"(?im)invalid header value[^\r\n]*", "Invalid header value [已隐藏]", text)
    return text.strip()[:1800]


@dataclass(frozen=True)
class ErrorHint:
    code: str
    title: str
    reason: str
    actions: tuple[str, ...]
    detail: str
    status: int | None = None

    def as_text(self, include_detail=False):
        text = f"{self.title}\n\n可能原因：{self.reason}\n\n建议：\n"
        text += "\n".join(f"• {action}" for action in self.actions)
        if self.status:
            try:
                phrase = HTTPStatus(self.status).phrase
            except ValueError:
                phrase = ""
            text += f"\n\n错误代码：HTTP {self.status} {phrase}".rstrip()
        if include_detail and self.detail:
            text += "\n\n详细错误：\n" + self.detail
        return text


class AnalysisError(ValueError):
    """Keep the useful failure from each lookup, without inventing a cause."""

    def __init__(self, errors):
        errors = errors or [ValueError("未找到可下载视频。网页未提供可识别的视频资源。")]
        hints = [explain_error(error, "analyze") for error in errors]
        primary = next((hint for hint in hints if hint.code not in ("unknown", "not_detected")), hints[0])
        details = list(dict.fromkeys(hint.detail for hint in hints if hint.detail))
        detail = "\n\n".join(f"查找记录 {index}：\n{item}" for index, item in enumerate(details, 1))[:1800]
        self.hint = replace(primary, detail=detail)
        super().__init__("未找到可下载视频。\n" + self.hint.as_text(include_detail=True))


def explain_error(error: BaseException, operation: str = "download") -> ErrorHint:
    if isinstance(error, AnalysisError):
        return error.hint
    chain, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or getattr(current, "cause", None) or current.__context__
    raw = "\n".join(str(item) for item in chain)
    # A video slug or signed query must not be treated as a reason/status.
    evidence = re.sub(r"https?://[^\s<>\"']+", "", raw)
    text = evidence.casefold()
    status = None
    for item in chain:
        response = getattr(item, "response", None)
        value = getattr(item, "status", None) or getattr(response, "status_code", None) or getattr(response, "status", None)
        if isinstance(value, int) and 400 <= value <= 599:
            status = value
            break
    if status is None:
        match = re.search(r"(?:\bHTTP(?:\s+Error|Error|\s+status)?[\s:=]+([45]\d\d)\b|\b([45]\d\d)\s+(?:Client|Server)\s+Error)", evidence, re.I)
        if match:
            status = int(match[1] or match[2])

    def hint(code, title, reason, *actions):
        return ErrorHint(code, title, reason, tuple(actions), redact_detail(error), status)

    win_errors = {getattr(item, "winerror", None) for item in chain}
    if any(word in text for word in ("未返回原视频", "无法唯一定位原视频", "视频已变化")):
        return hint("source_changed", "无法确认仍是原来的视频", "网页或接口返回的视频与原项目不一致，软件已停止该尝试。",
                    "重新查找原网页，并手动确认想下载的视频；软件不会自动选择其他视频替代。")
    if any(isinstance(item, InvalidMediaError) for item in chain):
        return hint("invalid_media", "服务器返回的内容不是可用视频", "下载地址可能已失效，或服务器返回了提示网页、空文件或损坏的数据。",
                    "重新查找原视频再下载；如果网站需要登录，请更新自己的登录信息。",
                    "无效素材不会用于下次续传；已完成的 MP4 不受影响。")
    if any(item.__class__.__name__ == "ProgressBusy" for item in chain):
        return hint("progress_busy", "保存位置有其他任务正在使用", "另一个软件窗口可能正在这个文件夹下载或清理未完成素材。",
                    "等待那个任务结束后重试，或换一个保存文件夹。", "本软件不会同时修改同一位置的下载进度。")
    cookie_code = getattr(error, "code", "")
    storage_error = next((item for item in chain if item.__class__.__name__ == 'StorageError'), None)
    if storage_error is not None:
        titles = {'disk_low': '保存盘空间不足，已停止后续任务',
                  'destination_unavailable': '保存位置暂时不可用',
                  'path_too_long': '保存路径过长', 'file_too_large': '此磁盘装不下这么大的单个文件'}
        return hint(storage_error.code, titles.get(storage_error.code, '保存位置需要调整'), str(storage_error),
                    '先处理磁盘空间或保存位置，再点“重试未完成”；不会自动重试整批。',
                    '勾选“保留进度”时，软件尽量保留可用素材；原位置和画质下重试会核对能否继续。更换目录暂不搬移旧素材。')
    if cookie_code == "browser_cookie_locked":
        return hint(cookie_code, "浏览器正在占用登录资料", str(error),
                    "先正常退出对应浏览器的所有窗口；若仍失败，检查任务管理器中是否还有该浏览器的后台进程。软件不会替你强制结束浏览器。",
                    "也可以保持浏览器打开，用可信扩展导出当前网站的 Cookies .txt，然后在“从文件导入”选择它。",
                    "在“文件导入”中点击“按步骤获取 Cookies”查看操作；也能从导出扩展复制后直接导入。浏览器内部数据库不能当作文本导入。")
    if cookie_code == "browser_cookie_encrypted":
        return hint(cookie_code, "浏览器加密阻止了直接读取", str(error),
                    "关闭浏览器也不一定能解决加密限制。可在已登录的网站中导出 Cookies .txt，再导入软件。",
                    "也可使用 Firefox 中自己的登录状态。无需管理员运行或修改浏览器安全设置。")
    if isinstance(cookie_code, str) and cookie_code.startswith("browser_cookie_"):
        return hint(cookie_code, "浏览器登录状态未就绪", str(error),
                    "确认选择的是已登录、能够播放该视频的浏览器及用户资料。",
                    "资料占用时可正常退出所选浏览器后再试；加密读取失败时可使用 Firefox 或导入当前网站的 Cookies 文件。",
                    "无需管理员运行或关闭浏览器安全功能；读取 Cookies 不能增加账号访问权限。")
    if any(getattr(item, "errno", None) == errno.ENOSPC for item in chain) or any(
            word in text for word in ("no space left", "disk full", "磁盘空间不足", "winerror 112")):
        return hint("disk_full", "保存空间不足", "保存盘或临时文件所在磁盘没有足够空间。",
                    "释放磁盘空间，或选择空间充足的保存文件夹。", "完成后重试失败的视频，已完成的文件不会被覆盖。")
    if any(getattr(item, 'errno', None) == errno.EFBIG for item in chain) or 223 in win_errors or 'file too large' in text:
        return hint('file_too_large', '文件超过磁盘允许的单文件大小', '保存盘或文件系统拒绝继续写入这么大的单个文件。',
                    '部分 FAT32 U 盘单文件上限约 4 GB。请改用 NTFS / exFAT 磁盘，或降低画质；软件不会格式化磁盘。')
    if win_errors & {15, 21, 55, 1117, 1167} or any(getattr(item, 'errno', None) in {errno.ENODEV, errno.ENXIO} for item in chain):
        return hint('destination_unavailable', '保存磁盘暂时不可用', '磁盘可能已断开、未就绪或发生读写错误。',
                    '重新连接磁盘并确认能在文件资源管理器中正常写入，再重试未完成的视频。')
    if "所选清晰度" in text or "requested format is not available" in text:
        return hint("quality_unavailable", "所选清晰度不可用", "源站的画质列表可能已经变化，或当前登录状态无法获取该画质。",
                    "重新解析网页，选择列表中实际提供的画质。", "可以先尝试较低清晰度；软件不会放大视频冒充高清。")
    if any(word in text for word in ("protected by a password", "video password", "--video-password", "视频密码")):
        return hint("password_required", "这个视频需要观看密码", "发布者为视频设置了独立的观看密码；这与浏览器登录状态不同。",
                    "先在原网页确认你拥有观看密码。当前版本没有视频密码输入功能。",
                    "重新读取登录信息不会代替观看密码，软件不会猜测密码或绕过该限制。")
    if "drm" in text or "受保护" in text:
        return hint("protected", "这个视频暂时无法下载", "网站对视频使用了加密保护（DRM），当前版本无法保存为普通 MP4。",
                    "使用网站自身提供的授权下载方式。")
    permission_text = re.sub(r"https?://[^\s<>\"']+", "", text)
    if any(word in permission_text for word in ("members-only", "members only", "subscriber-only", "subscribers-only", "vip required",
                                                "requires premium", "premium account required", "会员专享", "大会员专享", "需要大会员", "购买后观看", "充电专属")):
        return hint("membership_required", "网站提示需要会员或购买权限", "这个视频或画质要求对应的会员、订阅或单独购买权限。",
                    "先在浏览器中用自己的账号确认能否播放这个视频。",
                    "已有对应权限时，重新导入该账号的登录信息后查找；导入 Cookies 不会增加会员权限。")
    if any(word in text for word in ("cookies are no longer valid", "cookies have expired", "session has expired", "session expired")):
        return hint("session_expired", "登录状态可能已经失效", "站点没有接受导入的会话，Cookies 可能已过期，或账号需要重新登录验证。",
                    "先在浏览器中重新登录，确认这个视频能播放。",
                    "打开“登录与代理”，重新读取登录状态或导入最新 Cookies 文件，再查找视频。")
    if "fresh cookies" in text or "cookies (not necessarily logged in) are needed" in text:
        return hint("session_required", "网站需要更新的访问记录", "网站要求新的浏览器访问记录（Cookies），不一定要求账号登录，也可能暂时限制了下载工具。",
                    "先确认该网页可以在自己的浏览器中播放。", "在“登录与代理”中读取登录状态，或导入最新 Cookies 文件，再查找视频。",
                    "导入 Cookies 也不保证成功；站点改版或验证要求可能仍不受支持。")
    if any(word in text for word in ("isn't available to everyone", "certain audiences", "not available in your country", "geo restricted", "geo-restricted", "age restricted", "age-restricted")):
        return hint("audience_restricted", "这个视频限制了观看范围", "网站可能要求登录、年龄验证，或只允许部分地区和账号观看。",
                    "先用自己的浏览器和账号确认这个具体视频能否播放。", "如果可以播放，可导入自己的有效 Cookies 后重新解析。",
                    "如果浏览器也无法播放，软件无法提供当前账号无权访问的资源。")
    if status == 401 or (status not in (407, 429) and any(word in text for word in (
            "login required", "login_required", "please log in", "need to log in", "sign in to",
            "only works when logged-in", "must be logged in", "you are not logged in",
            "requires authentication", "authentication required", "authentication is required", "需要登录", "要求登录"))):
        return hint("authentication", "网站可能需要你先登录", "网站没有返回视频，可能需要登录或重新验证账号。",
                    "先确认自己的浏览器和账号可以播放该视频。", "在“登录与代理”中读取登录状态，或导入 Cookies 文件，再查找视频。")
    if status == 407 or any(word in text for word in ("proxyerror", "proxy error", "tunnel connection failed")):
        return hint("proxy", "代理连接失败", "配置的代理未正常转发请求，或代理要求身份验证。",
                    "检查“登录与代理”中的代理地址和端口，以及代理程序是否正在运行。", "如果没有使用代理，可清空代理输入框后重试。")
    if any(word in text for word in ("certificate verify failed", "certificate_verify_failed", "sslerror", "ssl certificate", "curl: (60)")):
        return hint("tls", "安全连接校验失败", "网站证书、系统时间或代理连接可能存在异常。",
                    "检查电脑日期、时间和代理状态。", "确认网址正确，并在浏览器中检查该网站是否能正常打开。")
    if any(word in text for word in ("getaddrinfo", "name or service not known", "failed to resolve", "could not resolve", "domain not found", "no such host")):
        return hint("dns", "无法找到目标服务器", "网址域名无法解析，可能是网络、代理或目标域名的问题。",
                    "确认网址没有拼写错误。", "检查浏览器是否能打开同一页面，以及当前网络或代理是否正常。")
    if status in (408, 504) or any(word in text for word in ("timed out", "timeout", "超时", "curl: (28)")):
        return hint("timeout", "连接或下载超时", "目标服务器响应较慢，或当前网络、代理不稳定。",
                    "检查网络和代理是否正常，稍后点击重试。", "较大视频可先选择较低清晰度验证。")
    if win_errors & {10013, 10051, 10054, 10060, 10061, 10065} or any(word in text for word in (
            "connection refused", "connection reset", "connection aborted", "network is unreachable", "remote end closed connection",
            "winerror 10013", "winerror 10051", "winerror 10054", "winerror 10061", "winerror 10065")):
        return hint("connection", "网络连接中断或无法建立", "本机网络、代理或目标服务器阻止了连接，当前不能仅凭此错误确定是哪一方。",
                    "检查同一网页能否在浏览器打开，确认网络或代理正常。", "网络恢复后重试失败视频。")
    if status == 429:
        return hint("rate_limited", "目标站点暂时限流", "站点认为当前网络出口的请求过多，暂时不提供资源。",
                    "稍后再试，减少连续解析或下载的次数。", "本次任务不会无限重试；已完成的视频会保留。")
    if status == 403:
        return hint("access_denied", "资源服务器拒绝访问", "可能是媒体链接过期、登录权限不足、请求来源限制或网络出口被拦截。",
                    "确认同一网络下，用自己的账号可以在浏览器中播放该视频。",
                    "网页视频会自动刷新地址一次；仍失败时可导入自己的有效 Cookies 后重新解析。")
    if status in (404, 410):
        return hint("unavailable", "资源不存在或地址已失效", "视频可能已被删除、移动，或媒体链接已经过期。",
                    "先在浏览器确认原视频仍然存在并能播放。", "重新粘贴原始视频网页解析；失效的直接媒体链接需要替换。")
    if status is None and any(word in text for word in ("this video is unavailable", "video unavailable")):
        return hint("unavailable", "网站暂未提供这个视频", "站点未返回可访问的视频；可能与原链接、当前网络或观看范围有关，不能仅凭这条提示确定视频已被删除。",
                    "先在浏览器确认同一视频是否可以播放。", "可以换一个公开视频链接验证；如果需要登录，再使用自己有效的登录信息。")
    if status in (412, 474):
        return hint("request_rejected", "媒体请求未通过站点校验", "站点拒绝了媒体请求，可能与请求来源、会话或网络环境有关。",
                    "使用原始播放页网址重新解析，避免使用过期的媒体直链。", "确认浏览器可以播放，并按需使用自己的有效 Cookies。")
    if status and status >= 500:
        return hint("server", "网站或网络节点暂时异常", "目标网站或中间代理返回了服务错误，可能是临时故障。",
                    "稍后重试，并检查同一页面能否在浏览器打开。", "如果多个网站都失败，优先检查自己的网络或代理。")
    if any(isinstance(item, PermissionError) or getattr(item, 'errno', None) == errno.EROFS for item in chain) or any(word in text for word in ("permission denied", "read-only file system", "拒绝访问", "access is denied")):
        return hint("save_permission", "无法写入本地文件", "保存目录没有写入权限，或文件正被其他程序占用。",
                    "选择自己可以写入的文件夹。", "如果视频正在被其他程序占用，关闭该文件后再重试。")
    if ("ffmpeg" in text and any(word in text for word in ("not found", "could be found", "找不到", "does not exist"))) or "impersonate target" in text:
        return hint("dependency", "视频处理组件不可用", "FFmpeg 或站点兼容依赖可能缺失、损坏或路径失效。",
                    "关闭软件，再从项目中的 start.bat 启动；若仍失败，请保留详细错误，检查或重新安装视频处理组件。")
    if any(word in text for word in ("mp4 转换失败", "invalid data found", "unknown encoder", "error while opening encoder", "error opening output")):
        return hint("conversion", "整理 MP4 文件时失败", "视频可能没有下载完整，或当前工具无法处理它的视频格式。",
                    "重新解析并下载，必要时换一个清晰度。", "如果重复失败，展开详细错误，保留该记录以便排查。")
    if "unsupported url" in text or "未找到可下载视频" in text or "no video formats" in text:
        return hint("not_detected", "没有识别到可下载的视频", "网页可能需要动态加载或登录，或当前解析器还不支持该页面。",
                    "使用具体视频的播放页链接重新解析。", "确认浏览器中能播放；登录资源可尝试自己的 Cookies。")
    if any(isinstance(item, FileNotFoundError) for item in chain):
        return hint("local_file", "本地文件或组件不存在", "选中的文件可能已被移动，或程序需要的本地组件不存在。",
                    "检查保存位置、Cookies 文件路径和项目依赖，然后重新启动。")
    return hint("unknown", "网页解析未完成" if operation == "analyze" else "下载未完成", "目前无法从错误中可靠判断具体原因。",
                "先确认网址正确、网络正常，再重新解析并重试。", "如果仍失败，可查看详细错误，提供该网址和错误记录以便进一步排查。")
