"""Stdlib-only launch guard, usable even when Qt cannot be imported."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
    executable = str(Path(sys.executable).resolve()).replace("'", "''")
    requirements = str(ROOT / 'requirements.txt').replace("'", "''")
    return ('先从项目文件夹双击 start.bat，避免使用了其他 Python 环境。\n'
            '如果仍提示缺少组件，可在 PowerShell 中用当前项目解释器安装项目依赖：\n'
            "& '" + executable + "' -m pip install -r '" + requirements + "'\n"
            '组件已经安装但无法载入时，请保留此提示；先修复该 Python 环境，不必重新下载视频或清除账号信息。')


def show_failure(problems, *, popup=True):
    message = '拾影暂时无法启动\n\n' + '\n'.join(problems) + '\n\n' + repair_steps()
    # Only the latest startup failure, no user URL, account data or environment dump.
    try:
        log = ROOT / '.local/startup.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        if not log.is_symlink() and not log.is_junction():
            log.write_text(message[:16000], encoding='utf-8')
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
                      '新解析引擎已安排恢复，请重新打开软件。' if recovered else '可能是组件文件损坏、运行库缺失或解释器不匹配。'], popup=popup)
        return 1
    try:
        return main()
    except Exception as exc:
        try:
            recovered = reject_start()
        except (EngineError, OSError):
            recovered = False
        show_failure(['打开应用时未完成初始化：' + type(exc).__name__ + '。',
                      '已安排恢复之前的解析引擎，请重新打开软件。' if recovered else '请保留本机启动记录，修复环境后重试；此提示不会清除已有视频或记录。'], popup=popup)
        return 1
