"""Opt-in queue snapshots containing original pages, never extracted media data."""
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path

from .core import MAX_VIDEOS, check_cancel, validate_url
from .errors import explain_error
from .history import video_identity
from .naming import NAMING_MODES
from .local_storage import replace_record

DEFAULT_QUEUE = Path(__file__).resolve().parent.parent / ".local/queue.json"
MAX_QUEUE_BYTES = 1024 * 1024


def job_key(item):
    return (item["url"], item["identity"], item["quality"])


def validate_items(items):
    if not isinstance(items, list) or len(items) > MAX_VIDEOS:
        raise ValueError("队列记录格式不正确。")
    clean, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("队列记录格式不正确。")
        url = validate_url(item.get("url", ""))
        identity, title, quality = item.get("identity"), item.get("title"), item.get("quality")
        filename = item.get("filename", "")
        if (len(url) > 8192 or not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity)
                or not isinstance(title, str) or len(title) > 300
                or quality is not None and (type(quality) is not int or not 0 < quality <= 16384)
                or not isinstance(filename, str) or len(filename) > 300):
            raise ValueError("队列记录格式不正确。")
        key = (url, identity, quality)
        if key not in seen:
            seen.add(key)
            clean.append({"url": url, "identity": identity, "title": title, "quality": quality})
            if filename:
                clean[-1]["filename"] = filename
    return clean


class QueueStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_QUEUE
        self.enabled = False
        self.snapshot = {"folder": "", "items": [], "naming_mode": "title_quality"}
        self.load_error = False
        try:
            if self.path.exists():
                with self.path.open("rb") as source:
                    raw = source.read(MAX_QUEUE_BYTES + 1)
                if len(raw) > MAX_QUEUE_BYTES:
                    raise ValueError
                data = json.loads(raw)
                if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or data.get("enabled") is not True
                        or not isinstance(data.get("folder"), str) or len(data["folder"]) > 32768 or "\x00" in data["folder"]):
                    raise ValueError
                mode = data.get("naming_mode", "title_quality")
                if mode not in NAMING_MODES:
                    raise ValueError
                self.snapshot = {"folder": data["folder"], "items": validate_items(data.get("items")), "naming_mode": mode}
                self.enabled = True
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            self.load_error = True

    def save(self, items, folder, *, naming_mode="title_quality"):
        if not self.enabled:
            return
        if not isinstance(naming_mode, str) or naming_mode not in NAMING_MODES:
            raise ValueError("文件命名规则不正确。")
        snapshot = {"folder": str(folder), "items": validate_items(items), "naming_mode": naming_mode}
        if len(snapshot["folder"]) > 32768 or "\x00" in snapshot["folder"]:
            raise ValueError("保存位置过长，无法记住队列。")
        if snapshot == self.snapshot and self.path.is_file() and not self.load_error:
            return
        data = {"version": 1, "enabled": True, **snapshot}
        content = json.dumps(data, ensure_ascii=False)
        if len(content.encode("utf-8")) > MAX_QUEUE_BYTES:
            raise ValueError("队列记录过大，请减少链接或缩短分享网址后再记住队列。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".queue-", suffix=".tmp", delete=False) as target:
                temp_path = Path(target.name)
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            replace_record(temp_path, self.path)
            self.snapshot = snapshot
            self.load_error = False
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    def clear(self):
        # Delete exactly this small snapshot, never videos or resume material.
        self.path.unlink(missing_ok=True)
        self.enabled = False
        self.snapshot = {"folder": "", "items": [], "naming_mode": "title_quality"}
        self.load_error = False

    def remove_items(self, indices):
        indices = set(indices)
        if not self.enabled or any(type(index) is not int or not 0 <= index < len(self.snapshot["items"]) for index in indices):
            raise ValueError("选择的队列项目已变化，请刷新窗口后重试。")
        removed = [item for index, item in enumerate(self.snapshot["items"]) if index in indices]
        remaining = [item for index, item in enumerate(self.snapshot["items"]) if index not in indices]
        self.save(remaining, self.snapshot["folder"], naming_mode=self.snapshot["naming_mode"])
        return removed


def restore_queue(snapshot, lookup, cancel, notify):
    """Look up each original page once, match identity and exact saved choice."""
    grouped = defaultdict(list)
    for item in validate_items(snapshot["items"]):
        grouped[item["url"]].append(item)
    videos, qualities, filenames, pending, issues = [], [], [], [], []
    for number, (url, jobs) in enumerate(grouped.items(), 1):
        check_cancel(cancel)
        notify("lookup", {"index": number, "total": len(grouped)})
        try:
            found = lookup(url)
            check_cancel(cancel)
        except Exception as exc:
            check_cancel(cancel)
            reason = explain_error(exc, "analyze").as_text()
            pending.extend(jobs)
            issues.extend(f"{job['title']}：\n{reason}" for job in jobs)
            continue
        identities = defaultdict(list)
        for video in found:
            identities[video_identity(video)].append(video)
        for job in jobs:
            matches = identities[job["identity"]]
            if len(matches) != 1:
                pending.append(job)
                issues.append(f"{job['title']}：没有唯一找到原视频，可能已被替换或删除。请手动查找确认。")
            elif job["quality"] is not None and job["quality"] not in matches[0].heights:
                pending.append(job)
                issues.append(f"{job['title']}：原选 {job['quality']}p 已不可用，请手动重新查找并选择画质。")
            else:
                videos.append(matches[0])
                qualities.append(job["quality"])
                filenames.append(job.get("filename", ""))
    return {"videos": videos, "qualities": qualities, "filenames": filenames, "pending": pending, "issues": issues,
            "folder": snapshot["folder"], "naming_mode": snapshot.get("naming_mode", "title_quality")}
