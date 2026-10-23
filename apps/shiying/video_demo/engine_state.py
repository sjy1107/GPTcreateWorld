"""Stdlib-only, atomic engine selection. The original environment stays intact."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from .paths import app_root, data_root, frozen

PROJECT = app_root()
ENGINE_ROOT = data_root() / 'engine'
SLOT = re.compile(r'v-[A-Za-z0-9._-]{1,80}-[0-9a-f]{12}')
OWNER = 'shiying-engine-v1'
_LEASES = {}


class EngineError(RuntimeError):
    pass


def root_path(root=None, *, create=False):
    path = Path(root or ENGINE_ROOT).absolute()
    for item in (path.parent, path):
        if item.is_symlink() or item.is_junction():
            raise EngineError('解析引擎目录是链接，暂不处理；现有环境保留。')
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def read_state(root=None):
    path = root_path(root) / 'state.json'
    initial = dict(schema=1, active='base', previous=None, pending=None, trial=None, notice='')
    if not path.exists():
        return initial
    if path.is_symlink() or path.is_junction() or path.stat().st_size > 16384:
        raise EngineError('解析引擎版本记录无法可靠读取，未执行更改。')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema') != 1:
            raise ValueError
        for key in ('active', 'previous', 'pending', 'trial'):
            value = data.get(key)
            if value is not None and value != 'base' and (not isinstance(value, str) or not SLOT.fullmatch(value)):
                raise ValueError
        if data.get('active') is None:
            raise ValueError
        initial.update({key: data.get(key) for key in ('active', 'previous', 'pending', 'trial')})
        initial['notice'] = str(data.get('notice') or '')[:500]
        return initial
    except (OSError, ValueError, TypeError, AttributeError):
        raise EngineError('解析引擎版本记录损坏，原环境仍保留。') from None


def write_state(state, root=None):
    root = root_path(root, create=True)
    target = root / 'state.json'
    if target.is_symlink():
        raise EngineError('版本记录是文件链接，未写入。')
    temporary = root / ('state-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def state_lock(root=None):
    root = root_path(root, create=True)
    target = root / '.lock'
    if target.is_symlink():
        raise EngineError('版本锁文件不可用。')
    with target.open('a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise EngineError('另一个窗口正在处理解析引擎，请稍后再试。') from None
        try:
            yield root
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def slot_path(slot, root=None):
    root = root_path(root)
    if slot == 'base':
        return None
    if not isinstance(slot, str) or not SLOT.fullmatch(slot):
        raise EngineError('解析引擎版本标识不正确。')
    path = root / slot
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        raise EngineError('选中的解析引擎文件不可用。')
    marker = path / 'engine.json'
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
        raise EngineError('选中的解析引擎缺少验证记录。')
    try:
        info = json.loads(marker.read_text(encoding='utf-8'))
        if info.get('owner') != OWNER or info.get('slot') != slot or not info.get('validated'):
            raise ValueError
    except (ValueError, AttributeError):
        raise EngineError('无法确认这份版本属于本软件，未启用。') from None
    return path


def _lease(slot, root, pid=None):
    target = root_path(root) / ('.lease-' + slot + '-' + str(pid or os.getpid()))
    if target.is_symlink() or target.is_junction():
        raise EngineError('版本使用记录不可用。')
    handle = target.open('a+b')
    if handle.seek(0, 2) == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        raise


def in_use(slot, root):
    if (str(root_path(root)), slot) in _LEASES:
        return True
    for path in root_path(root).glob('.lease-' + slot + '-*'):
        pid = path.name.rsplit('-', 1)[-1]
        if not pid.isdigit():
            continue
        try:
            handle = _lease(slot, root, pid)
            handle.close()
        except OSError:
            return True
    return False


def probe(slot, *, full=False):
    """Run fresh imports and compatibility checks in a child, never reload Qt."""
    command = ('import sys; sys.path.insert(0, sys.argv[1]); '
               'sys.path.insert(0, sys.argv[2]) if sys.argv[2] else None; '
               'from video_demo.engine_probe import main; main(sys.argv[3])')
    env = dict(os.environ, YTDLP_NO_PLUGINS='1', PYTHONUTF8='1', NO_PROXY='127.0.0.1,localhost')
    try:
        if frozen():
            worker = PROJECT / '_internal/ShiyingWorker.exe'
            if not worker.is_file():
                raise EngineError('缺少后台验证程序，请完整解压免安装包后重试。')
            args = [str(worker), '--engine-probe', str(slot or ''), 'full' if full else 'quick']
        else:
            args = [sys.executable, '-I', '-c', command, str(PROJECT), str(slot or ''), 'full' if full else 'quick']
        result = subprocess.run(args,
                                capture_output=True, timeout=100 if full else 25, env=env,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise EngineError('本机版本验证超过时间限制，未启用新版；原引擎保留。') from None
    if result.returncode:
        try:
            detail = json.loads(result.stdout.decode('utf-8')).get('error', '')[:1000]
        except (ValueError, UnicodeError, AttributeError, TypeError):
            detail = ''
        raise EngineError('新版本未通过本机兼容验证，现有版本保持不变。' + detail)
    try:
        report = json.loads(result.stdout.decode('utf-8'))
        source = report.pop('module_path', '')
        if slot and (not source or not Path(source).resolve().is_relative_to(Path(slot).resolve())):
            raise EngineError('验证时没有实际载入选中的新版，未启用；原引擎保留。')
        return report
    except (ValueError, UnicodeError):
        raise EngineError('新版本验证结果不完整，未启用。') from None


def schedule_restore(root=None):
    with state_lock(root):
        try:
            state = read_state(root)
        except EngineError:
            state = dict(schema=1, active='base', previous=None, pending='base', trial=None,
                         notice='版本记录已修复，下次启动使用原始项目引擎；托管文件与视频保留。')
            write_state(state, root)
            return state
        if state['pending'] is not None:
            state.update(pending=None, notice='已取消待启用版本；当前运行版本保持。')
        elif state['previous'] is not None and state['previous'] != state['active']:
            slot_path(state['previous'], root)
            state.update(pending=state['previous'], notice='已安排恢复之前的版本，关闭并重新打开软件后生效。')
        else:
            raise EngineError('尚无可恢复版本；当前仍使用原环境。')
        write_state(state, root)
        return state


def prepare_startup(root=None):
    """Validate before imports; interrupted first launches revert next time."""
    if not (root_path(root) / 'state.json').exists():
        return ''
    with state_lock(root):
        state = read_state(root)
        if state['trial'] is not None:
            state.update(active=state['previous'] or 'base', trial=None,
                         notice='上次新版启动未完成，已恢复之前的解析引擎。')
        if state['pending'] is not None:
            state.update(previous=state['active'], active=state['pending'], trial=state['pending'], pending=None)
        selected = state['active']
        try:
            path = slot_path(selected, root)
            if path:
                probe(path)
        except Exception:
            selected = state['previous'] or 'base'
            state.update(active=selected, trial=None, notice='新版文件或兼容检查未通过，已恢复之前的引擎。')
            try:
                path = slot_path(selected, root)
                if path:
                    probe(path)
            except Exception:
                path = None
                state.update(active='base', notice='托管版本不可用，已回到项目原始引擎。')
        if state['trial'] is not None:
            state['notice'] = ('本次已载入安排的解析引擎，原环境和之前的版本保留。' if state['active'] != 'base'
                               else '本次已回到项目原始解析引擎，视频与账号信息保留。')
        write_state(state, root)
        if path:
            key = (str(root_path(root)), state['active'])
            if key not in _LEASES:
                _LEASES[key] = _lease(state['active'], root)
    if path:
        sys.path.insert(0, str(path))
    return state['notice']


def confirm_start(root=None):
    if not (root_path(root) / 'state.json').exists():
        return
    with state_lock(root):
        state = read_state(root)
        if state['trial'] is not None:
            state.update(trial=None, notice='解析引擎已成功启动，之前的版本仍可恢复。')
            write_state(state, root)


def reject_start(root=None):
    if not (root_path(root) / 'state.json').exists():
        return False
    with state_lock(root):
        state = read_state(root)
        if state['active'] == 'base':
            return False
        state.update(active=state['previous'] or 'base', pending=None, trial=None,
                     notice='新引擎启动失败，已安排恢复；请重新打开软件。')
        write_state(state, root)
        return True
