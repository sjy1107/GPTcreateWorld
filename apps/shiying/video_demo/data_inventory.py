"""Bounded, read-only inventory of known local data and exact-file clearing."""
from dataclasses import dataclass
from pathlib import Path
import os

from .health import history_count, read_preferences
from .queue_store import QueueStore
from .resume import list_progress


@dataclass(frozen=True)
class DataItem:
    key: str
    title: str
    location: str
    size: int | None
    summary: str
    detail: str
    view: str = ''
    clear: str = ''


def record_bytes(path):
    total = 0
    for candidate in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm'), Path(str(path) + '-journal')):
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError('记录路径包含链接，未沿链接统计。')
        if candidate.exists():
            if not candidate.is_file():
                raise ValueError('记录位置不是普通文件。')
            total += candidate.stat().st_size
    return total


def clear_record_file(path):
    path = Path(path)
    if path.suffix.lower() not in ('.json', '.log') or path.is_symlink() or path.is_junction():
        raise ValueError('无法确认是可清除的记录文件，未执行操作。')
    path.unlink(missing_ok=True)


def output_summary(folder, limit=5000):
    if not folder.is_dir():
        return 0, 0, False
    size = count = 0
    limited = False
    with os.scandir(folder) as files:
        for number, entry in enumerate(files):
            if number >= limit:
                limited = True
                break
            if entry.name.lower().endswith('.mp4') and entry.is_file(follow_symlinks=False):
                size += entry.stat(follow_symlinks=False).st_size
                count += 1
    return count, size, limited


def inventory(folder, preferences, history, queue, startup_log, *, has_session=False, cookie_path='', log_lines=0):
    folder = Path(folder).expanduser().resolve()
    rows = []
    definitions = [
        ('preferences', '记住的保存位置', preferences, 'preferences',
         '仅记录保存目录，不含网址或账号。清除后本次目录保持；下次启动回到默认目录。之后选择目录或下载时会重新记住。'),
        ('history', '下载历史', history, 'history',
         '包含成功视频的标题、文件位置、画质、时间和少量内容指纹；不含原网页或登录信息。清空只删除记录，视频原文件保留。数据库文件保留供下次使用，体积不一定立即缩小。'),
        ('queue', '记住的任务队列', queue, 'queue',
         '主动开启后保存原始网页链接、标题、所选画质及命名设置。清除会关闭任务记忆，不删除视频或续传素材；当前主列表仍保留。'),
        ('startup_log', '最近一次启动失败记录', startup_log, '',
         '仅在启动失败时保存一份提示，之后成功启动不会自动删除旧记录。没有原视频链接、Cookies 或完整环境变量。清除只删除这一份启动记录。'),
    ]
    for key, title, path, view, note in definitions:
        try:
            size = record_bytes(path)
            if key == 'history':
                summary = str(history_count(path)) + ' 条记录'
            elif key == 'queue':
                store = QueueStore(path)
                if store.load_error:
                    raise ValueError('记录无法读取')
                summary = str(len(store.snapshot['items'])) + ' 项，已开启记忆' if store.enabled else '未开启记忆'
            elif key == 'preferences':
                summary = read_preferences(path)
            else:
                summary = '有一份历史启动提示（不代表现在仍有问题）' if path.exists() else '尚无启动失败记录'
        except (OSError, ValueError, RuntimeError) as exc:
            size = None
            summary = '暂时无法读取'
            note += '\n可能是记录损坏、被占用或没有权限。可先关闭其他软件窗口再试；不要删除视频来修复记录。'
        except Exception:
            size = None
            summary = '暂时无法读取'
            note += '\n记录可能被占用或损坏，先保留文件，再查看环境检查或复制检查摘要排查。'
        rows.append(DataItem(key, title, str(path), size, summary, note, view, key))
    try:
        progress = list_progress(folder)
        size = sum(item['bytes'] for item in progress)
        summary = str(len(progress)) + ' 份保留素材'
    except (OSError, ValueError, RuntimeError):
        size, summary = None, '暂时无法统计，可能被另一任务使用'
    rows.append(DataItem('partials', '未完成的下载素材', str(folder / '.shiying-partials'), size, summary,
                         '只统计当前保存位置下本软件保留的进度目录。清理时需要另行勾选并确认；会失去对应素材或续传内容，已完成 MP4 保留。其他保存目录需切换后查看。', 'partials', 'partials'))
    try:
        count, size, limited = output_summary(folder)
        summary = str(count) + ' 个当前目录中的 MP4' + ('（达到枚举上限，仅统计部分）' if limited else '')
    except (OSError, ValueError):
        size, summary = None, '暂时无法统计'
    rows.append(DataItem('outputs', '已完成的 MP4 文件', str(folder), size, summary,
                         '仅统计当前文件夹第一层的普通 .mp4 文件，不检查视频内容、不遍历子文件夹、不跟随文件链接，最多枚举 5000 项。这些文件可能包含你手动放入的视频；此页只读，不提供删除。', 'outputs'))
    rows.append(DataItem('session', '软件暂存的登录信息', cookie_path or '本次运行的内存中；任务临时副本用后清理', None,
                         '已添加，未检查内容' if has_session or cookie_path else '未添加',
                         '检查不会读取浏览器或导出文件内容，也不将外部凭据文件计入软件占用。清除仅取消软件中的会话和文件选择，不退出浏览器账号、不删除用户导出的 Cookies 文件。', 'login', 'session'))
    rows.append(DataItem('log', '当前窗口运行记录', '仅在当前窗口内存中', None, '约 ' + str(log_lines) + ' 行运行记录',
                         '最多保留 500 行，关闭窗口后不保存。清空不删除视频、历史、任务或启动失败记录。', '', 'log'))
    engine_root = Path(history).parent / 'engine'
    try:
        from .engine_state import read_state
        state = read_state(engine_root)
        summary = '有待启用或恢复安排' if state['pending'] is not None else '使用原环境' if state['active'] == 'base' else '使用独立验证的版本'
    except Exception:
        summary = '版本记录暂时不可读，可到管理页修复'
    rows.append(DataItem('engine', '解析引擎版本与恢复记录', str(engine_root), None, summary,
                         '新版放在此项目的独立目录，原 Python 环境保留。当前、之前和待启用版本会保留；只在明确更新时清理确认属于软件且没有窗口使用的旧版本。此页不直接删除引擎文件，打开管理页可检查更新、取消安排或恢复版本。', 'engine'))
    return rows
