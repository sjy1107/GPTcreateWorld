"""Explicit local-media acceptance for frozen builds. Never run automatically."""
import argparse
import functools
import http.server
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from .paths import app_root, data_root, prepare_portable, frozen
from .version import VERSION


def _run(*args):
    from .core import ffmpeg_path, CREATE_NO_WINDOW
    result = subprocess.run([ffmpeg_path(), '-hide_banner', '-nostdin', '-y', *map(str, args)],
                            capture_output=True, timeout=35, creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-1800:])
    return result


def _fixtures(root):
    _run('-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24', '-f', 'lavfi', '-i',
         'sine=frequency=440:sample_rate=44100', '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast',
         '-pix_fmt', 'yuv420p', '-c:a', 'aac', root / 'first.mp4')
    _run('-i', root / 'first.mp4', '-vf', 'scale=320:180', '-c:v', 'libvpx-vp9', '-deadline',
         'realtime', '-cpu-used', '8', '-c:a', 'libopus', root / 'second.webm')
    for height in (180, 360):
        _run('-i', root / 'first.mp4', '-vf', f'scale=-2:{height}', '-c:v', 'libx264', '-preset',
             'ultrafast', '-g', '24', '-c:a', 'aac', '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0', root / f'hls_{height}.m3u8')
    _run('-i', root / 'first.mp4', '-c', 'copy', '-f', 'dash', '-seg_duration', '1',
         '-init_seg_name', str(root / 'init-stream$RepresentationID$.m4s'),
         '-media_seg_name', str(root / 'chunk-stream$RepresentationID$-$Number%05d$.m4s'),
         '-adaptation_sets', 'id=0,streams=v id=1,streams=a', root / 'manifest.mpd')
    manifest = root / 'manifest.mpd'
    manifest.write_text(manifest.read_text(encoding='utf-8').replace(str(root) + '\\', '').replace(str(root) + '/', ''), encoding='utf-8')
    (root / 'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x180\nhls_180.m3u8\n'
                                   '#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360\nhls_360.m3u8\n', encoding='utf-8')
    (root / 'index.html').write_text('<html><meta charset="utf-8"><title>免安装版本地验收</title>'
                                   '<video title="MP4"><source src="first.mp4"></video>'
                                   '<video title="WebM"><source src="second.webm"></video>'
                                   '<video title="HLS"><source src="master.m3u8"></video>'
                                   '<video title="DASH"><source src="manifest.mpd"></video></html>', encoding='utf-8')


def _wait(app, predicate, seconds=60):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError('Qt task did not finish')


def _smoke(report, destination):
    from PySide6.QtWidgets import QApplication
    from .core import ffmpeg_path, js_runtimes
    from .health import environment_checks
    from . import gui
    from . import preferences
    # No browser profile inventory or account read is needed for local fixtures.
    gui.discover_profiles = lambda *_: []
    app = QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFont, QFontDatabase
    if os.environ.get('QT_QPA_PLATFORM') == 'offscreen':
        # Qt's offscreen platform may omit the native Windows font database.
        # Register the OS-provided font for QA only; no Windows font is shipped.
        font = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts/msyh.ttc'
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont('Microsoft YaHei UI', 10))
    app.setStyle('Fusion')
    checks = environment_checks(app_root() / 'downloads', preferences.PREFERENCES,
                                data_root() / 'history.sqlite3', data_root() / 'queue.json')
    report['environment'] = {check.key: check.state for check in checks}
    assert not [check.key for check in checks if check.state == 'error'], report['environment']
    assert report['environment']['runtime'] == 'ok', 'Bundled JS runtime not usable'
    assert Path(ffmpeg_path()).is_relative_to(app_root())
    assert Path(js_runtimes()['deno']['path']).is_relative_to(app_root())
    report['external_python_required'] = not frozen()
    report['tools_in_portable_folder'] = True
    window = gui.MainWindow()
    window.error_dialog = lambda *args: errors.append(str(args[:2]))
    errors = []
    with tempfile.TemporaryDirectory(prefix='acceptance-') as temporary:
        root = Path(temporary)
        _fixtures(root)
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            window.show()
            app.processEvents()
            window.url.setText(f'http://127.0.0.1:{server.server_port}/index.html')
            window.analyze_button.click()
            _wait(app, lambda: window.worker is None)
            assert len(window.videos) == 4, (len(window.videos), errors, window.log.toPlainText())
            output = root / 'saved'
            window.folder.setText(str(output))
            speed = []
            original = window.handle_event
            def event(kind, value):
                original(kind, value)
                if kind == 'progress' and value.get('speed'):
                    speed.append(value['speed'])
            window.handle_event = event
            for row, video in enumerate(window.videos):
                if video.heights and 180 in video.heights:
                    combo = window.table.cellWidget(row, 3)
                    combo.setCurrentIndex(combo.findData(180))
            window.download_button.click()
            _wait(app, lambda: window.worker is None, seconds=120)
            assert all(value == '已完成' for value in window.states.values()), (window.states, errors, window.log.toPlainText())
            media = list(output.glob('*.mp4'))
            assert len(media) == 4
            for path in media:
                text = subprocess.run(
                    [ffmpeg_path(), '-hide_banner', '-i', str(path)], capture_output=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stderr.decode('utf-8', 'replace')
                assert 'Video: h264' in text and 'Audio: aac' in text
                _run('-v', 'error', '-xerror', '-err_detect', 'explode', '-i', path, '-f', 'null', '-')
            report.update(qt_analysis=True, qt_four_downloads=True, mp4_hls_dash_webm=True,
                          all_mp4_full_decode=True, gui_errors=errors, live_speed_events=len(speed),
                          history_records=len(window.history.recent()), app_title=window.windowTitle())
            assert not errors
            window.grab().save(str(destination.with_suffix('.png')))
            from .usage_guide import UsageGuide
            guide = UsageGuide(window, topic='portable', availability=window.guide_availability)
            guide.show()
            app.processEvents()
            report['portable_help_topic'] = any(topic.key == 'portable' for topic in guide.topics)
            guide.close()
            from .system_dialog import SystemDialog
            dialog = SystemDialog(window, tab=2)
            dialog.show()
            app.processEvents()
            report['engine_update_tab_loaded'] = True
            dialog.grab().save(str(destination.with_name(destination.stem + '-engine.png')))
            dialog.close()
            _wait(app, lambda: not dialog.isVisible())
        finally:
            window.close()
            _wait(app, lambda: not window.isVisible())
            server.shutdown()
            server.server_close()
            thread.join()


def _startup(report, destination):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    app = QApplication.instance() or QApplication([])
    launched = time.monotonic()
    errors = []
    def inspect_and_close():
        try:
            from .gui import MainWindow
            windows = [window for window in app.topLevelWidgets() if isinstance(window, MainWindow)]
            assert len(windows) == 1
            window = windows[0]
            if window.health_worker or window.storage_worker:
                if time.monotonic() - launched > 25:
                    raise TimeoutError('Startup checks did not finish')
                QTimer.singleShot(100, inspect_and_close)
                return
            import yt_dlp
            from .engine_state import read_state
            report.update(real_bootstrap_gui=True, engine_path=str(Path(yt_dlp.__file__).resolve()),
                          engine_version=importlib.metadata.version('yt-dlp'), engine_state=read_state(),
                          title=window.windowTitle(), startup_seconds=round(time.monotonic() - launched, 3))
            window.grab().save(str(destination.with_suffix('.png')))
            window.close()
        except Exception:
            errors.append(traceback.format_exc())
            app.exit(1)
    QTimer.singleShot(200, inspect_and_close)
    from .bootstrap import launch
    code = launch(popup=False)
    if code or errors:
        raise RuntimeError(str(errors) or str(code))


def _imports(report):
    """Import the real GUI stack without opening windows or reading accounts."""
    report['imports'] = []
    for name in ('PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'video_demo.gui'):
        module = importlib.import_module(name)
        report['imports'].append({'module': name, 'file': str(module.__file__)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--operation', choices=['imports', 'smoke', 'startup', 'engine-stage', 'engine-restore'], default='smoke')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    destination = Path(args.report).resolve()
    report = {'version': VERSION, 'operation': args.operation, 'execution_mode': 'frozen' if frozen() else 'source', 'external_video_requests': False,
              'real_browser_credentials_read': False, 'status': 'failed'}
    try:
        prepare_portable()
        if args.operation == 'startup':
            _startup(report, destination)
        else:
            from .engine_state import prepare_startup
            prepare_startup()
            if args.operation == 'imports':
                _imports(report)
            elif args.operation == 'smoke':
                _smoke(report, destination)
            elif args.operation == 'engine-stage':
                from .engine_update import check_update, install_candidate
                candidate = check_update('preview')['candidate']
                assert candidate is not None
                report['installed_engine'] = install_candidate(candidate, root=data_root() / 'engine',
                                                              cancel=threading.Event(), progress=lambda *_: None)
            else:
                from .engine_state import schedule_restore
                report['restore'] = schedule_restore()
        report['status'] = 'passed'
    except Exception:
        report['error'] = traceback.format_exc()
        if sys.platform == 'win32':
            # Query this diagnostic process only; never inspect a browser or
            # enumerate another process's memory, credentials or environment.
            try:
                import ctypes
                from ctypes import wintypes
                process = ctypes.windll.kernel32.GetCurrentProcess
                process.restype = wintypes.HANDLE
                handles = (wintypes.HMODULE * 2048)()
                needed = wintypes.DWORD()
                enum = ctypes.windll.psapi.EnumProcessModules
                enum.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
                get_name = ctypes.windll.psapi.GetModuleFileNameExW
                get_name.argtypes = (wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
                enum(process(), handles, ctypes.sizeof(handles), ctypes.byref(needed))
                libraries = []
                for handle in handles[:min(len(handles), needed.value // ctypes.sizeof(wintypes.HMODULE))]:
                    name = ctypes.create_unicode_buffer(32768)
                    if get_name(process(), handle, name, len(name)) and any(
                            token in Path(name.value).name.lower() for token in ('qt6', 'pyside', 'shiboken', 'msvcp', 'vcruntime', 'python')):
                        libraries.append(name.value)
                report['loaded_native_libraries'] = libraries
            except Exception:
                pass
    destination.parent.mkdir(exist_ok=True, parents=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['status'] == 'passed' else 1
