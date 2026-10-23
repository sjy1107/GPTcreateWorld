"""Bounded retries using yt-dlp callbacks, with interruptible local countdowns."""
from __future__ import annotations

import math
import threading
import time
from email.utils import parsedate_to_datetime

import yt_dlp
from yt_dlp.networking.exceptions import CertificateVerifyError, HTTPError, TransportError


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("任务已取消")


def retry_after(headers, *, now=None):
    """Return a nonnegative delay, including values above our auto-wait limit."""
    value = headers.get("Retry-After") if headers else None
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        if value.strip().isdigit():
            return float(value.strip())
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return max(0, stamp.timestamp() - (time.time() if now is None else now))
    except (ValueError, TypeError, OverflowError):
        return None


class RetryController:
    MAX_WAIT = 60

    def __init__(self, notify, cancel):
        self.notify, self.cancel = notify, cancel
        self.local = threading.local()

    def observe(self, error):
        self.local.error = error

    def wait(self, seconds, reason, attempt, kind):
        check_cancel(self.cancel)
        self.notify("log", {"text": f"{reason}，等待 {math.ceil(seconds)} 秒后重试（第 {attempt} 次）；可以取消。"})
        deadline = time.monotonic() + seconds
        previous = None
        while True:
            check_cancel(self.cancel)
            remaining = max(0, math.ceil(deadline - time.monotonic()))
            if remaining != previous:
                self.notify("progress", {"phase": "等待重试", "percent": None,
                                         "retry_seconds": remaining, "retry_reason": reason,
                                         "retry_attempt": attempt, "retry_kind": kind})
                previous = remaining
            if not remaining:
                return
            # Also works with the combined cancel/skip event used by the queue.
            self.cancel.wait(min(0.2, max(0, deadline - time.monotonic())))

    def delay(self, kind, n=0):
        check_cancel(self.cancel)
        error = None if kind == "file_access" else getattr(self.local, "error", None)
        status = getattr(error, "status", None)
        # Permission errors require user action/URL refresh, not blind retries.
        if (status in (401, 403, 404, 410) or isinstance(error, CertificateVerifyError)
                or getattr(error, "_shiying_retry_exhausted", False)):
            raise error
        seconds = min(8, (1 if kind in ("fragment", "file_access") else 2) * 2 ** min(n, 3))
        reason = (f"服务器暂时出错（HTTP {status}）" if status and 500 <= status < 600
                  else "文件暂时被占用" if kind == "file_access" else "连接暂时中断或响应超时")
        if isinstance(error, HTTPError):
            requested = retry_after(error.response.headers)
            if requested is not None:
                if requested > self.MAX_WAIT:
                    self.notify("log", {"text": "网站要求较长时间后再试，已停止自动重试。请稍后手动重试。"})
                    raise error
                seconds = max(seconds, requested)
            # The retry decision is made; release this failed response before
            # waiting so cancel/skip cannot leave its socket open.
            error.close()
        self.wait(seconds, reason, n + 1, kind)
        # Wait above so cancellation stays responsive; yt-dlp must not sleep again.
        return 0

    def callbacks(self):
        return {kind: (lambda n=0, kind=kind: self.delay(kind, n))
                for kind in ("http", "fragment", "extractor", "file_access")}


class RetryYoutubeDL(yt_dlp.YoutubeDL):
    def urlopen(self, request):
        controller = self.params.get("_shiying_retry")
        if controller is None:
            return super().urlopen(request)
        method = getattr(request, "method", "GET")
        safe = method in ("GET", "HEAD") and not getattr(request, "data", None)
        # Native HTTP retries 5xx/transport errors; only supplement HTTP 429,
        # which native HTTP does not retry. Never automatically replay POSTs.
        for attempt in range(3):
            check_cancel(controller.cancel)
            try:
                response = super().urlopen(request)
            except (HTTPError, TransportError) as error:
                controller.observe(error)
                if not (safe and isinstance(error, HTTPError) and error.status == 429):
                    raise
                delay = retry_after(error.response.headers)
                if attempt == 2 or (delay is not None and delay > controller.MAX_WAIT):
                    error._shiying_retry_exhausted = True
                    controller.notify("log", {"text": "网站限制了请求频率（HTTP 429），已停止自动重试，请稍后再试。"})
                    raise
                seconds = max(5 * (attempt + 1), delay or 0)
                error.close()
                controller.wait(seconds, "网站要求放慢请求（HTTP 429）", attempt + 1, "http")
            else:
                controller.observe(None)
                return response
