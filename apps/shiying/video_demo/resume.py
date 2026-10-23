"""Conservative local resume storage. Manifests never contain URLs or cookies."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from types import SimpleNamespace
from contextlib import contextmanager
from pathlib import Path

import yt_dlp
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.networking import Request

from .history import video_identity
from .network_retry import RetryYoutubeDL

CACHE_NAME = ".shiying-partials"
MANIFEST = "progress.json"
VERSION = 1


class ProgressBusy(RuntimeError):
    pass


class ResumeChanged(RuntimeError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def job_key(video, height):
    return digest([video_identity(video), height])


def _check_path(path: Path, parent: Path):
    if path.is_symlink() or path.is_junction() or path.resolve().parent != parent.resolve():
        raise ValueError("未完成文件目录包含链接或不在保存位置内，请换一个保存文件夹。")


def _check_tree(path: Path):
    for child in path.iterdir():
        _check_path(child, path)
        if child.is_dir():
            _check_tree(child)


def _read_manifest(path: Path):
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return {}
        if not isinstance(data.get("files"), dict) or not isinstance(data.get("validators"), dict):
            return {}
        if not isinstance(data.get("updated"), (int, float)) or not isinstance(data.get("title"), str):
            return {}
        if any(not isinstance(v, dict) for v in data["validators"].values()):
            return {}
        return data
    except (OSError, ValueError):
        return {}


@contextmanager
def progress_lock(folder: Path):
    folder = folder.expanduser().resolve()
    root = folder / CACHE_NAME
    _check_path(root, folder)
    root.mkdir(exist_ok=True)
    lock_path = root / ".lock"
    _check_path(lock_path, root)
    handle = lock_path.open("a+b")
    locked = False
    try:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise ProgressBusy("这个保存位置正在被另一个下载或清理操作使用，请等它结束，或选择其他保存文件夹。") from None
        yield root
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _snapshot(path):
    _check_tree(path)
    return {item.name: [item.stat().st_size, item.stat().st_mtime_ns]
            for item in path.iterdir() if item.is_file() and item.name not in (MANIFEST, MANIFEST + ".tmp")}


def _strong_tag(value):
    return isinstance(value, str) and len(value) <= 1024 and bool(re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value))


def _response_identity(response):
    size = response.headers.get("Content-Length")
    if response.status == 206:
        matched = re.fullmatch(r"bytes \d+-\d+/(\d+)", response.headers.get("Content-Range", ""))
        size = matched[1] if matched else None
    return {"etag": response.headers.get("ETag"), "size": int(size) if size and size.isdigit() else None,
            "final": digest(response.url)}


class ProgressWorkspace:
    def __init__(self, video, height, folder, notify):
        self.key = job_key(video, height)
        self.video, self.height, self.folder, self.notify = video, height, folder, notify
        self.data = {}
        self.success = False
        self.discard = False
        self.persistent = True
        self.tracks = {}
        self.resumable = False
        self.resuming = False

    def __enter__(self):
        self.lock = progress_lock(self.folder)
        self.root = self.lock.__enter__()
        try:
            self.path = self.root / self.key
            _check_path(self.path, self.root)
            self.path.mkdir(exist_ok=True)
            _check_tree(self.path)
            self.data = _read_manifest(self.path / MANIFEST)
            return self
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise

    def log(self, text):
        self.notify("log", {"text": text})

    def clear(self):
        _check_path(self.path, self.root)
        _check_tree(self.path)
        for child in self.path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        self.data = {}
        self.resuming = False

    def save(self):
        self.data.update(version=VERSION, key=self.key, title=self.video.title[:200], quality=self.height,
                         updated=time.time(), files=_snapshot(self.path))
        target = self.path / MANIFEST
        temporary = self.path / (MANIFEST + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    def prepare(self, preview):
        streams = preview.get("requested_formats") or [preview]
        # Exact resource and format fingerprints. Rotated signed URLs, a new
        # quality, a different codec or a modified fragment list start afresh.
        fields = ("format_id", "url", "manifest_url", "protocol", "ext", "width", "height",
                  "vcodec", "acodec", "filesize", "fragments", "fragment_base_url", "http_headers")
        fingerprint = digest([{key: stream.get(key) for key in fields} for stream in streams])
        self.tracks = {digest(stream["url"]): stream for stream in streams if stream.get("url")}
        self.resumable = bool(streams) and all(stream.get("protocol") in ("http", "https")
                                              and not stream.get("fragments") and not stream.get("request_data")
                                              and not any(str(key).lower() in ("range", "if-range") for key in (stream.get("http_headers") or {}))
                                              for stream in streams)
        old_files = self.data.get("files")
        valid = (self.data.get("key") == self.key and self.data.get("fingerprint") == fingerprint
                 and isinstance(old_files, dict) and old_files == _snapshot(self.path))
        if self.data and not valid:
            self.log("之前的下载资源或本地进度已变化，将重新下载，避免拼接错文件。")
        if not valid:
            self.clear()
        self.data.update(fingerprint=fingerprint, resumable=self.resumable)
        self.data.setdefault("validators", {})
        self.resuming = bool(valid and old_files)
        self.save()

    def ready_source(self):
        name = self.data.get("ready")
        if not isinstance(name, str) or Path(name).name != name or not name.startswith("source."):
            return None
        path = self.path / name
        if path.is_file() and path.stat().st_size > 0:
            self.log("视频素材已下载完整，继续整理为 MP4，无需再次传输。")
            return path
        return None

    def save_ready(self, path):
        if path.parent != self.path:
            raise ValueError("下载文件不在预期的临时目录中。")
        self.data["ready"] = path.name
        self.save()

    def verify(self, ydl, cancel_check):
        if not self.resuming:
            return
        validators = self.data.get("validators", {})
        if not self.resumable or not validators or any(not _strong_tag(v.get("etag")) or not v.get("size") for v in validators.values()):
            self.log("这个资源暂时无法可靠校验旧进度，将从头下载；已完成的 MP4 不受影响。")
            self.restart()
            return
        self.log("发现未完成文件，正在核对服务器上的视频是否仍是同一份…")
        self.notify("progress", {"phase": "核对下载进度", "percent": None})
        for key, expected in list(validators.items()):
            cancel_check()
            stream = self.tracks.get(key)
            if not stream:
                self.restart()
                return
            headers = dict(ydl.params.get("http_headers") or {})
            headers.update(stream.get("http_headers") or {})
            headers.update({"Range": "bytes=0-0", "If-Range": expected["etag"], "Accept-Encoding": "identity"})
            extension = {"impersonate": ydl.params["impersonate"]} if ydl.params.get("impersonate") else {}
            with super(ResumeYoutubeDL, ydl).urlopen(Request(stream["url"], headers=headers, extensions=extension)) as response:
                current = _response_identity(response)
                if response.status != 206 or current != expected:
                    self.log("服务器上的文件已变化，或不支持接着传输，将重新下载。")
                    self.restart()
                    return
        cancel_check()
        self.log("校验通过，接着下载已保留的进度。")

    def restart(self):
        fingerprint = self.data.get("fingerprint")
        self.clear()
        self.data.update(fingerprint=fingerprint, resumable=self.resumable, validators={})
        self.save()

    def __exit__(self, kind, error, traceback):
        try:
            if self.success or self.discard or isinstance(error, ResumeChanged):
                self.clear()
                self.path.rmdir()
            elif self.data:
                converted = self.path / "converted.mp4"
                if converted.exists():
                    converted.unlink()
                validators = self.data.get("validators", {})
                keep = self.data.get("ready") or (self.resumable and validators and all(_strong_tag(v.get("etag")) and v.get("size") for v in validators.values()))
                if keep and _snapshot(self.path):
                    self.save()
                    self.log("未完成的素材已保存在保存位置，下次选择同一视频与画质时会核对进度后继续。")
                else:
                    self.clear()
                    self.path.rmdir()
                    self.log("当前资源无法可靠续传，已清理未完成片段；重试时会重新下载。")
            else:
                self.clear()
                self.path.rmdir()
        except (OSError, ValueError):
            if error is None and not self.success:
                raise
            self.log("暂时未能完整保存或清理下载进度；下次会重新检查，不影响已完成的 MP4。")
        finally:
            self.lock.__exit__(kind, error, traceback)


class SafeYoutubeDL(RetryYoutubeDL):
    def dl(self, name, info, subtitle=False, test=False):
        if test:
            return super().dl(name, info, subtitle, test)
        if not info.get("url"):
            self.raise_no_formats(info, True)
        downloader = get_suitable_downloader(info, self.params, to_stdout=(name == "-"))(self, self.params)
        for hook in self._progress_hooks:
            downloader.add_progress_hook(hook)
        opened = []
        original_open = downloader.sanitize_open
        def tracked_open(*args, **kwargs):
            stream, filename = original_open(*args, **kwargs)
            opened.append(stream)
            return stream, filename
        # The pinned native fragment downloader does not close its aggregate
        # file when a progress hook raises our cancellation exception. Track
        # only this downloader instance's files; never patch shared globals.
        downloader.sanitize_open = tracked_open
        try:
            item = self._copy_infodict(info)
            if item.get("http_headers") is None:
                item["http_headers"] = self._calc_headers(item)
            return downloader.download(name, item, subtitle)
        finally:
            for stream in reversed(opened):
                if not stream.closed:
                    stream.close()


class ResumeYoutubeDL(SafeYoutubeDL):
    def __init__(self, params, workspace):
        self.workspace = workspace
        super().__init__(params)

    def urlopen(self, request):
        if not isinstance(request, Request):
            return super().urlopen(request)
        key = digest(request.url)
        workspace = self.workspace
        tracked = workspace.resumable and key in workspace.tracks and request.method == "GET"
        previous = workspace.data.get("validators", {}).get(key) if tracked else None
        ranged = bool(re.match(r"bytes=[1-9]\d*-", request.headers.get("Range", "")))
        if tracked and ranged:
            if previous and _strong_tag(previous.get("etag")):
                request.headers["If-Range"] = previous["etag"]
            else:
                # Native HTTP resets its local partial when a full response is
                # returned. Never append a response that cannot be identified.
                request.headers.pop("Range", None)
                request.headers.pop("If-Range", None)
        response = super().urlopen(request)
        if tracked:
            current = _response_identity(response)
            if ranged and previous and _strong_tag(previous.get("etag")) and current != previous:
                response.close()
                raise ResumeChanged("下载过程中服务器文件发生变化，需重新下载。")
            workspace.data.setdefault("validators", {})[key] = current
            workspace.save()
        return response


@contextmanager
def download_workspace(video, height, folder, notify, enabled):
    if enabled:
        with ProgressWorkspace(video, height, folder, notify) as workspace:
            yield workspace
    else:
        with tempfile.TemporaryDirectory(prefix=".video-work-", dir=folder) as directory:
            yield SimpleNamespace(path=Path(directory), persistent=False)


def list_progress(folder):
    folder = Path(folder).expanduser().resolve()
    root = folder / CACHE_NAME
    _check_path(root, folder)
    if not root.is_dir():
        return []
    rows = []
    with progress_lock(folder):
        for path in root.iterdir():
            if not re.fullmatch(r"[0-9a-f]{64}", path.name):
                continue
            _check_path(path, root)
            if not path.is_dir():
                continue
            _check_tree(path)
            data = _read_manifest(path / MANIFEST)
            size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            if data.get('key') != path.name:
                rows.append({'key': path.name, 'title': '无法核对的旧素材', 'quality': None,
                             'bytes': size, 'updated': path.stat().st_mtime,
                             'state': '记录不完整，暂不清理', 'can_clear': False})
                continue
            rows.append({"key": path.name, "title": str(data.get("title") or "未命名视频"),
                         "quality": data.get("quality"), "bytes": size, "updated": data.get("updated", 0),
                         "state": "等待整理 MP4" if data.get("ready") else "重试时核对后继续"})
    return sorted(rows, key=lambda row: row["updated"], reverse=True)


def clear_progress(folder, keys):
    folder = Path(folder).expanduser().resolve()
    with progress_lock(folder) as root:
        for key in keys:
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ValueError("下载进度记录不正确，未执行清理。")
            path = root / key
            _check_path(path, root)
            if not path.exists():
                continue
            _check_tree(path)
            if _read_manifest(path / MANIFEST).get("key") != key:
                raise ValueError("无法确认这份进度属于软件，未执行清理。")
            shutil.rmtree(path)
