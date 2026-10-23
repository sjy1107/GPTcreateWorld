"""Offline environment checks. No account extraction or automatic repairs."""
from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .storage import MIN_FREE, preflight_destination


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    state: str
    summary: str
    detail: str
    action: str = ''


def read_preferences(path):
    from .preferences import MAX_BYTES
    if not path.exists():
        return '尚未记住保存位置；首次使用时正常。'
    with path.open('rb') as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('记录过大')
    data = json.loads(raw)
    folder = data.get('folder') if isinstance(data, dict) else None
    if not isinstance(folder, str) or not folder.strip() or len(folder) > 32768 or '\x00' in folder:
        raise ValueError('记录格式不正确')
    return '保存位置记录可以读取。'


def history_count(path, *, integrity=False):
    if not path.exists():
        return 0
    if path.stat().st_size > 32 * 1024**2:
        raise ValueError('记录超过检查上限，请先保留文件排查。')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.5)) as db:
        deadline = time.monotonic() + 1
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if integrity and db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
            raise ValueError('记录完整性检查未通过')
        columns = {row[1] for row in db.execute('PRAGMA table_info(downloads)')}
        if not {'id', 'identity', 'title', 'path', 'quality', 'size', 'finished'} <= columns:
            raise ValueError('历史记录表结构不完整')
        return db.execute('SELECT COUNT(*) FROM downloads').fetchone()[0]


def run_program(command, timeout=6):
    return subprocess.run(command, capture_output=True, timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def runtime_check():
    from .core import js_runtimes
    try:
        from yt_dlp.utils._jsruntime import DenoJsRuntime, NodeJsRuntime
        deno_minimum = DenoJsRuntime.MIN_SUPPORTED_VERSION
        node_minimum = NodeJsRuntime.MIN_SUPPORTED_VERSION
    except (ImportError, AttributeError):
        deno_minimum, node_minimum = (2, 3, 0), (22, 0, 0)
    candidates = js_runtimes()
    node = candidates.get('node', {}).get('path')
    deno = shutil.which('deno')
    for name, executable, minimum in (('Deno', deno, deno_minimum), ('Node.js', node, node_minimum)):
        if not executable:
            continue
        try:
            result = run_program([str(executable), '--version'], timeout=3)
            text = result.stdout.decode('utf-8', 'replace').strip().splitlines()[0]
            number = re.search(r'(\d+)\.(\d+)\.(\d+)', text)
            if result.returncode == 0 and number and tuple(map(int, number.groups())) >= minimum:
                return Check('runtime', '部分网站的脚本支持', 'ok', name + ' 可用',
                             '检测到 ' + name + '：' + text + '\n只运行了版本查询，没有执行网站脚本。')
        except (OSError, subprocess.TimeoutExpired, IndexError):
            continue
    required = 'Deno ' + '.'.join(map(str, deno_minimum)) + '+ 或 Node.js ' + '.'.join(map(str, node_minimum)) + '+'
    return Check('runtime', '部分网站的脚本支持', 'warning', '未找到符合要求的脚本运行组件',
                 '普通视频仍可尝试。当前解析器需要 ' + required + '。YouTube 等部分网站可能需要此可选组件，安装后重新检查。软件不会自动下载安装。')


def environment_checks(folder, preferences, history, queue, *, ffmpeg=None):
    from .core import ffmpeg_path
    from .queue_store import QueueStore
    checks = [Check('python', 'Python 运行环境', 'ok' if sys.version_info[:2] >= (3, 12) else 'error',
                    sys.version.split()[0], '当前解释器：' + sys.executable + '\n本项目使用 Python 3.12 验证。')]
    for name, title in (('PySide6', '界面组件'), ('yt-dlp', '视频解析组件'), ('requests', '网络组件'),
                        ('beautifulsoup4', '网页读取组件'), ('imageio-ffmpeg', '自带视频组件'),
                        ('curl-cffi', '部分网站的连接支持'), ('yt-dlp-ejs', '部分网站的脚本解析支持')):
        try:
            version = importlib.metadata.version(name)
            checks.append(Check(name, title, 'ok', version, '组件版本：' + version + '\n这里只检查本机安装情况，不查询更新。'))
        except importlib.metadata.PackageNotFoundError:
            checks.append(Check(name, title, 'warning' if name in ('curl-cffi', 'yt-dlp-ejs') else 'error', '未检测到组件',
                                '请从 start.bat 启动；如果仍缺失，可使用项目解释器安装 requirements.txt 中的依赖。'))
    try:
        executable = ffmpeg or ffmpeg_path()
        result = run_program([str(executable), '-version'])
        if result.returncode:
            raise OSError('version failed')
        codecs = run_program([str(executable), '-hide_banner', '-encoders'])
        if codecs.returncode:
            raise OSError('encoders failed')
        output = codecs.stdout.decode('utf-8', 'replace')
        complete = bool(re.search(r'\blibx264\b', output) and re.search(r'\baac\b', output))
        version = result.stdout.decode('utf-8', 'replace').splitlines()[0]
        checks.append(Check('ffmpeg', 'MP4 视频处理', 'ok' if complete else 'warning',
                            '可运行，常用编码齐全' if complete else '能运行，但缺少常用编码',
                            '处理组件：' + str(executable) + '\n' + version + '\n'
                            + ('已检测到 H.264 与 AAC 输出编码。' if complete else '部分原格式视频可能仍可保存；需要转换编码的视频可能失败，请准备包含 libx264 / AAC 的 FFmpeg。')))
    except (OSError, RuntimeError, subprocess.TimeoutExpired, IndexError):
        checks.append(Check('ffmpeg', 'MP4 视频处理', 'error', '处理组件无法运行',
                            '当前不能可靠整理 MP4。先从 start.bat 重启；若仍失败，检查系统 FFmpeg 是否损坏，或重新安装项目的 imageio-ffmpeg 组件，然后点击“重新检查”。'))
    checks.append(runtime_check())
    try:
        if not str(folder).strip():
            raise ValueError('empty folder')
        folder = Path(folder).expanduser().resolve()
        existing = folder.exists()
        if existing:
            volume = preflight_destination(folder)
            free = shutil.disk_usage(folder).free
            checks.append(Check('folder', '当前保存位置', 'ok' if free >= MIN_FREE else 'warning',
                                '可以写入' if free >= MIN_FREE else '剩余空间过低',
                                str(folder) + '\n可用字节：' + str(free) + '\n文件系统：' + (volume.filesystem or '未识别')
                                + '\n已用小文件检查写入、刷新和改名；下载前仍会按视频大小再次检查。', 'folder'))
        else:
            parent = folder.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if not parent.is_dir():
                raise OSError('destination parent unavailable')
            preflight_destination(parent)
            checks.append(Check('folder', '当前保存位置', 'info', '文件夹尚未创建',
                                str(folder) + '\n新位置尚不存在，已检查最近的现有上级目录可以写入，但没有创建目标目录。开始下载时会尝试创建并再次检查；也可先更改位置。', 'folder'))
    except (OSError, ValueError, RuntimeError):
        checks.append(Check('folder', '当前保存位置', 'warning', '当前不能写入',
                            '请确认磁盘已连接、位置是文件夹且允许写入；可以更改保存位置后重新检查。', 'folder'))
    # Probe the nearest existing parent without creating persistent data folders.
    try:
        parent = preferences.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        preflight_destination(parent)
        checks.append(Check('records_dir', '本机记录的保存权限', 'ok', '上级目录可以写入',
                            str(preferences.parent) + '\n新记录将在需要时创建；检查只写入并移除了探测文件，不代表未来所有写入都能成功。', 'data'))
    except (OSError, ValueError, RuntimeError):
        checks.append(Check('records_dir', '本机记录的保存权限', 'warning', '可能无法记住设置',
                            '项目或记录目录暂时不可写；视频仍可尝试保存到可写目录，但设置、历史或队列可能无法保存。', 'data'))
    for key, title, action in (('preferences', '保存位置记录', 'data'), ('history', '下载历史记录', 'data'), ('queue', '任务队列记录', 'data')):
        try:
            if key == 'preferences':
                summary = read_preferences(preferences)
            elif key == 'history':
                summary = '可读取，共 ' + str(history_count(history, integrity=True)) + ' 条；没有记录时为首次使用的正常状态。'
            else:
                store = QueueStore(queue)
                if store.load_error:
                    raise ValueError('queue invalid')
                summary = '已记住 ' + str(len(store.snapshot['items'])) + ' 项。' if store.enabled else '任务记忆未开启；默认如此。'
            checks.append(Check(key, title, 'ok', summary, summary + '\n检查不会更改或清除原记录。', action))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            if getattr(exc, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                summary = '记录被其他窗口占用'
                detail = '先正常关闭其他拾影窗口，再点击“重新检查”。不需要为此清空记录或删除视频。'
            elif key == 'history':
                summary = '历史记录无法识别或读取'
                detail = '先确认记录目录可访问，并关闭其他拾影窗口后重试。若仍失败，请在“本机数据”打开所在文件夹，退出软件并保留 / 备份历史文件供排查。视频原文件不受影响；不要删除视频来修复记录。'
            else:
                summary = '记录格式不正确或暂时不可读'
                detail = '先检查目录权限并关闭其他拾影窗口。若确认旧记录已损坏，可在“本机数据”阅读清除影响后只清除这类记录；不会删除视频或自动重置其他设置。'
            checks.append(Check(key, title, 'warning', summary, detail, action))
    return checks
