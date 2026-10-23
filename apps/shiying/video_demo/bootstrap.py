"""Stdlib-only launch guard, usable even when Qt cannot be imported."""
import importlib.util
import os
import re
import sys
from pathlib import Path
from .paths import app_root, frozen, prepare_portable

ROOT = app_root()
ENGINE_NOTICE = ''
ESSENTIAL = (('PySide6', '界面组件'), ('yt_dlp', '视频解析组件'),
             ('imageio_ffmpeg', '视频处理组件'), ('requests', '网络组件'), ('bs4', '网页读取组件'))


def dependency_issues():
    problems = []
    if sys.version_info[:2] < (3, 12):
        problems.append('Python 版本过旧，请使用 Python 3.12 或更新版本；本项目在 3.12 中验证。')
    for module, title in ESSENTIAL:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, OSError):
            found = False
        if not found:
            problems.append(title + '未安装（' + module + '）。')
    return problems


def repair_steps():
    if frozen():
        return ('请关闭软件，重新下载 Windows 免安装版并完整解压，再双击“拾影.exe”。\n'
                '请保留 _internal、tools 和 portable.flag，不要只复制 exe。\n'
                '若 data 无法写入，请将整个文件夹移到自己的可写目录；无需以管理员身份运行。\n'
                '旧版的 data 与下载视频请先保留，不要覆盖或删除。')
    executable = str(Path(sys.executable).resolve()).replace("'", "''")
    requirements = str(ROOT / 'requirements.txt').replace("'", "''")
    return ('先从项目文件夹双击 start.bat，避免使用了其他 Python 环境。\n'
            '如果仍提示缺少组件，可在 PowerShell 中用当前项目解释器安装项目依赖：\n'
            "& '" + executable + "' -m pip install -r '" + requirements + "'\n"
            '组件已经安装但无法载入时，请保留此提示；先修复该 Python 环境，不必重新下载视频或清除账号信息。')


def failure_details(error):
    """Keep useful import/frame evidence, never arbitrary errors or locals."""
    lines = ['错误类型：' + type(error).__name__]
    component = getattr(error, 'name', None)
    if isinstance(component, str) and re.fullmatch(r'[A-Za-z0-9_.]{1,160}', component):
        lines.append('组件：' + component)
    winerror = getattr(error, 'winerror', None)
    if isinstance(winerror, int):
        lines.append('Windows 错误代码：' + str(winerror))
    if isinstance(error, ImportError):
        dll = re.fullmatch(r'DLL load failed while importing ([A-Za-z0-9_.]+): ([^\r\n]{1,240})', str(error))
        if dll and not re.search(r'https?://|cookie|authorization', dll[2], re.I):
            lines.append('组件加载信息：' + dll[0])
    trace = error.__traceback__
    for _ in range(20):
        if trace is None:
            break
        frame = trace.tb_frame.f_code
        lines.append(f'位置：{Path(frame.co_filename).name}:{trace.tb_lineno} · {frame.co_name}')
        trace = trace.tb_next
    return '\n'.join(lines)


def show_failure(problems, *, popup=True, error=None):
    message = '拾影暂时无法启动\n\n' + '\n'.join(problems) + '\n\n' + repair_steps()
    # Technical details stay in the local log; no source lines, locals,
    # arbitrary exception messages, user URLs, account data or environment dump.
    try:
        log = ROOT / ('data' if frozen() else '.local') / 'startup.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        if not log.is_symlink() and not log.is_junction():
            details = '\n\n供排查的组件记录：\n' + failure_details(error) if error is not None else ''
            log.write_text((message + details)[:16000], encoding='utf-8')
            message += '\n\n本机启动记录：' + str(log)
    except (OSError, AttributeError):
        pass
    try:
        print(message, file=sys.stderr)
    except (OSError, UnicodeError):
        pass
    if popup and os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, '拾影 · 启动需要处理', 0x10)
        except Exception:
            pass


def launch(*, popup=True):
    global ENGINE_NOTICE
    try:
        prepare_portable()
    except OSError:
        show_failure(['软件的数据目录不能写入。请将完整文件夹移到自己的可写位置。'], popup=popup)
        return 1
    from .engine_state import EngineError, prepare_startup, reject_start
    try:
        ENGINE_NOTICE = prepare_startup()
    except (EngineError, OSError):
        ENGINE_NOTICE = '解析引擎版本记录暂时不可用，本次使用项目原环境。可到“检查与数据 → 解析引擎更新”修复版本记录。'
    issues = dependency_issues()
    if issues:
        show_failure(issues, popup=popup)
        return 1
    try:
        from .gui import main
    except Exception as exc:
        try:
            recovered = reject_start()
        except (EngineError, OSError):
            recovered = False
        show_failure(['组件存在，但未能载入：' + type(exc).__name__ + '。',
                      '新解析引擎已安排恢复，请重新打开软件。' if recovered else '可能是组件文件损坏、运行库缺失或解释器不匹配。'], popup=popup, error=exc)
        return 1
    try:
        return main()
    except Exception as exc:
        try:
            recovered = reject_start()
        except (EngineError, OSError):
            recovered = False
        show_failure(['打开应用时未完成初始化：' + type(exc).__name__ + '。',
                      '已安排恢复之前的解析引擎，请重新打开软件。' if recovered else '请保留本机启动记录，修复环境后重试；此提示不会清除已有视频或记录。'], popup=popup, error=exc)
        return 1
