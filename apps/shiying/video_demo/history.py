"""Local successful-download history; no source URLs or credentials are stored."""
import hashlib
import json
import sqlite3
import stat
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from .browser_cookies import site_scope
from .paths import data_root, encode_path, decode_path

DEFAULT_HISTORY = data_root() / 'history.sqlite3'


def file_signature(status):
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)


def sampled_fingerprint(path):
    """At most three 64 KiB samples; not a full-file integrity certificate."""
    path = Path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise ValueError('请选择一个非空的 MP4 文件。')
    size = before.st_size
    hasher = hashlib.sha256(f'v1:{size}'.encode())
    with path.open('rb') as source:
        for offset in sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)}):
            source.seek(offset)
            hasher.update(str(offset).encode() + b':' + source.read(65536))
    if file_signature(before) != file_signature(path.stat()):
        raise OSError('检查时文件发生了变化，请等文件复制或修改完成后再试。')
    return 'v1:' + hasher.hexdigest(), file_signature(before)


@dataclass(frozen=True)
class RelocationCandidate:
    path: Path
    fingerprint: str
    signature: tuple
    matched: bool


def inspect_relocation(record, path):
    """Local-only validation, called in a worker before asking for confirmation."""
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != '.mp4':
        raise ValueError('请选择之前保存的 MP4 文件，不要选择网页、快捷方式或未完成的素材。')
    if path.stat().st_size != record['size']:
        raise ValueError('所选文件大小与原记录不同，未更新记录。请选择之前下载的视频原文件。')
    fingerprint, signature = sampled_fingerprint(path)
    expected = record.get('fingerprint')
    if expected and expected != fingerprint:
        raise ValueError('所选文件的内容指纹与原记录不同，未更新记录。即使大小相同，也可能不是原视频。')
    from .core import inspect_local_media
    media = inspect_local_media(path)
    if not media.get('video_codec'):
        raise ValueError('所选文件没有识别到视频画面，可能不是可用的 MP4，未更新记录。')
    if file_signature(path.stat()) != signature:
        raise OSError('检查时文件发生了变化，请稍后再试。')
    return RelocationCandidate(path, fingerprint, signature, bool(expected))


def file_state(record):
    """An inexpensive location/size check, not a media integrity guarantee."""
    try:
        status = Path(record["path"]).stat()
        if not stat.S_ISREG(status.st_mode):
            return "missing"
        return "ready" if status.st_size == record["size"] and status.st_size > 0 else "changed"
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError, TypeError):
        return "unreadable"


def stable_url(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_")]
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme.lower(), host, parts.path, urlencode(sorted(query)), ""))


def video_identity(video):
    info = video.info
    extractor = str(info.get("extractor_key") or info.get("extractor") or "generic").lower()
    if extractor not in ("generic", "html5mediaembed") and info.get("id"):
        data = [extractor, str(info["id"]), site_scope(video.referer) if video.referer else ""]
    else:
        urls = {stable_url(fmt.get("manifest_url") or fmt["url"]) for fmt in info.get("formats", [])
                if fmt.get("url") and fmt.get("vcodec") != "none"}
        if not urls and info.get("url"):
            urls.add(stable_url(info["url"]))
        data = ["generic", sorted(urls), stable_url(video.referer) if not urls else ""]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


class HistoryStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_HISTORY

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE IF NOT EXISTS downloads (id INTEGER PRIMARY KEY, identity TEXT NOT NULL, title TEXT NOT NULL, path TEXT NOT NULL, quality INTEGER NOT NULL, size INTEGER NOT NULL, finished TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS download_identity ON downloads(identity, quality)")
            if 'fingerprint' not in {row[1] for row in db.execute('PRAGMA table_info(downloads)')}:
                try:
                    db.execute('ALTER TABLE downloads ADD COLUMN fingerprint TEXT')
                except sqlite3.OperationalError:
                    if 'fingerprint' not in {row[1] for row in db.execute('PRAGMA table_info(downloads)')}:
                        raise
        except Exception:
            db.close()
            raise
        return db

    def record(self, video, quality, path):
        path = Path(path).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            return
        fingerprint, signature = sampled_fingerprint(path)
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO downloads(identity,title,path,quality,size,finished,fingerprint) VALUES(?,?,?,?,?,?,?)",
                       (video_identity(video), video.title, encode_path(path), quality or 0, signature[2],
                        datetime.now(timezone.utc).isoformat(), fingerprint))
            db.execute("DELETE FROM downloads WHERE id NOT IN (SELECT id FROM downloads ORDER BY id DESC LIMIT 500)")

    def recent(self):
        if not self.path.exists():
            return []
        with closing(self.connect()) as db:
            records = [dict(row) for row in db.execute("SELECT * FROM downloads ORDER BY id DESC LIMIT 500")]
            for record in records:
                stored = record['path']
                record['path'] = decode_path(stored)
                if stored != record['path']:
                    record['_stored_path'] = stored
            return records

    def find(self, video, quality):
        return self.find_many([(0, video, quality)]).get(0)

    def find_many(self, items):
        if not items or not self.path.exists():
            return {}
        requests = [(row, (video_identity(video), quality or 0)) for row, video, quality in items]
        wanted = {key for _, key in requests}
        found = {}
        # One connection and one bounded read for the entire queue. A locked
        # history must not cause the GUI to wait once per selected video.
        for record in self.recent():
            key = (record["identity"], record["quality"])
            if key not in wanted or key in found:
                continue
            try:
                if file_state(record) == "ready":
                    found[key] = record
            except OSError:
                continue
        return {row: found[key] for row, key in requests if key in found}

    def remove(self, record_id=None):
        if not self.path.exists():
            return
        with closing(self.connect()) as db, db:
            if record_id is None:
                db.execute("DELETE FROM downloads")
            else:
                db.execute("DELETE FROM downloads WHERE id=?", (record_id,))

    def remove_many(self, record_ids):
        ids = list(record_ids)
        if any(type(value) is not int or value <= 0 for value in ids):
            raise ValueError("下载记录编号无效。")
        if not ids or not self.path.exists():
            return
        with closing(self.connect()) as db, db:
            db.executemany("DELETE FROM downloads WHERE id=?", [(value,) for value in set(ids)])

    def relocate(self, record, candidate):
        if file_signature(candidate.path.stat()) != candidate.signature:
            raise ValueError('确认期间文件发生了变化，未更新记录，请重新检查。')
        with closing(self.connect()) as db, db:
            cursor = db.execute('UPDATE downloads SET path=?, fingerprint=? WHERE id=? AND path=? AND size=? AND fingerprint IS ?',
                                (encode_path(candidate.path), candidate.fingerprint, record['id'], record.get('_stored_path', record['path']),
                                 record['size'], record.get('fingerprint')))
            if cursor.rowcount != 1:
                raise ValueError('这条历史记录已被修改或移除，请刷新列表后重试。')
