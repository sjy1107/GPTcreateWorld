"""Fresh-process, account-free engine contracts and optional real MP4 smoke test."""
import functools
import http.server
import importlib.metadata
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import sys


class ProbeFailure(RuntimeError):
    pass


def _validate(mode='quick'):
    import yt_dlp
    from pip._vendor.packaging.requirements import Requirement
    from .core import Network, PageYoutubeDL, analyze, download, ffmpeg_path, options
    from .format_policy import format_selector
    from .site_compat import DouyinPublicIE
    for text in importlib.metadata.requires('yt-dlp') or []:
        requirement = Requirement(text)
        if any(requirement.marker is None or requirement.marker.evaluate({'extra': extra}) for extra in ('', 'default', 'curl-cffi')):
            try:
                installed = importlib.metadata.version(requirement.name)
            except importlib.metadata.PackageNotFoundError:
                raise ProbeFailure(f'缺少新版需要的附加组件 {requirement.name}。请先安装对应组件；原引擎保留。') from None
            if requirement.specifier and not requirement.specifier.contains(installed, prereleases=True):
                raise ProbeFailure(f'新版需要 {requirement.name} {requirement.specifier}，当前为 {installed}。请先更新该附加组件；原引擎保留。')
    cancel = threading.Event()
    opts = options(lambda *_: None, cancel)
    opts.update(format=format_selector(360), check_formats=False)
    info = {'id': 'probe', 'title': 'Probe', 'formats': [
        {'format_id': 'video', 'url': 'https://invalid.test/video', 'height': 360, 'ext': 'mp4', 'vcodec': 'h264', 'acodec': 'none'},
        {'format_id': 'audio', 'url': 'https://invalid.test/audio', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'aac'}]}
    with PageYoutubeDL(opts) as ydl:
        result = ydl.process_ie_result(info, download=False)
        assert [item['format_id'] for item in result['requested_formats']] == ['video', 'audio']
        assert callable(ydl.add_info_extractor) and callable(DouyinPublicIE._parse_aweme_video_app)
    from yt_dlp.version import __version__
    from pip._vendor.packaging.version import Version
    distribution_version = importlib.metadata.version('yt-dlp')
    if Version(__version__).base_version != Version(distribution_version).base_version:
        raise ProbeFailure('解析引擎代码与版本记录不一致，请重新下载；原环境保留。')
    report = {'version': distribution_version, 'contracts': True, 'mp4': False,
              'module_path': str(Path(yt_dlp.__file__).resolve())}
    if mode == 'full':
        from .core import CREATE_NO_WINDOW
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'sample.mp4'
            command = [ffmpeg_path(), '-hide_banner', '-nostdin', '-y', '-f', 'lavfi', '-i',
                       'testsrc2=size=160x90:rate=12', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                       '-t', '1', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)]
            subprocess.run(command, check=True, capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW)
            class Handler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_):
                    pass
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                video = analyze(f'http://127.0.0.1:{server.server_port}/sample.mp4', cancel, lambda *_: None)[0]
                path = download(video, None, root / 'output', cancel, lambda *_: None, Network())
                assert path.suffix == '.mp4'
                subprocess.run([ffmpeg_path(), '-v', 'error', '-xerror', '-err_detect', 'explode', '-i', str(path), '-f', 'null', '-'],
                               check=True, capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW)
                report['mp4'] = True
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
    print(json.dumps(report))


def main(mode='quick'):
    try:
        _validate(mode)
    except Exception as error:
        detail = str(error)[:1000] if isinstance(error, ProbeFailure) else '本机接口或 MP4 测试未通过，错误类型：' + type(error).__name__
        print(json.dumps({'error': detail}, ensure_ascii=False))
        raise SystemExit(1)
