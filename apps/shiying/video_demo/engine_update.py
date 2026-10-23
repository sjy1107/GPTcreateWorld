"""Manual PyPI-only wheel staging; do not alter packages in the running venv."""
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import time
from urllib.parse import urlsplit
import uuid
import zipfile

from .engine_state import (EngineError, OWNER, in_use, probe, read_state, root_path,
                           slot_path, state_lock, write_state)
from .network_retry import check_cancel

MAX_WHEEL = 16 * 1024 * 1024


@dataclass(frozen=True)
class Candidate:
    version: str
    filename: str
    url: str
    sha256: str
    size: int
    channel: str


def versions():
    from packaging.version import Version, InvalidVersion
    from packaging.specifiers import SpecifierSet
    return Version, InvalidVersion, SpecifierSet


def current_version():
    import importlib.metadata
    return importlib.metadata.version('yt-dlp')


def _session(proxy='', url='https://pypi.org'):
    import requests
    session = requests.Session()
    # Honor network proxy settings without requests automatically reading netrc
    # account credentials for the release host.
    session.trust_env = False
    session.proxies.update(requests.utils.get_environ_proxies(url))
    if proxy:
        session.proxies.update(http=proxy, https=proxy)
    session.headers.update({'Accept': 'application/json', 'User-Agent': 'Shiying-Engine-Check/1'})
    return session


def check_update(channel='stable', proxy='', cancel=None):
    if channel not in ('stable', 'preview'):
        raise EngineError('请选择稳定版或预发布版。')
    if cancel:
        check_cancel(cancel)
    Version, InvalidVersion, SpecifierSet = versions()
    try:
        with _session(proxy) as session, session.get('https://pypi.org/pypi/yt-dlp/json', stream=True,
                                                    timeout=(10, 15), allow_redirects=False) as response:
            response.raise_for_status()
            payload = bytearray()
            for chunk in response.iter_content(65536):
                if cancel:
                    check_cancel(cancel)
                payload.extend(chunk)
                if len(payload) > 8 * 1024 * 1024:
                    raise EngineError('版本列表过大，已停止检查。')
            metadata = json.loads(payload)
    except EngineError:
        raise
    except Exception as error:
        from .network_retry import Cancelled
        if isinstance(error, Cancelled):
            raise
        raise EngineError('暂时无法连接官方版本服务。请检查网络或代理，稍后再试；当前引擎没有改变。') from None
    found = []
    for version, artifacts in metadata.get('releases', {}).items():
        try:
            parsed = Version(version)
        except InvalidVersion:
            continue
        if parsed.is_prerelease and channel == 'stable':
            continue
        for item in artifacts:
            filename = item.get('filename', '')
            url = item.get('url', '')
            digest = (item.get('digests') or {}).get('sha256', '')
            requirement = item.get('requires_python')
            if (item.get('yanked') or not re.fullmatch(r'yt_dlp-[A-Za-z0-9._+]+-py3-none-any\.whl', filename)
                    or urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'files.pythonhosted.org'
                    or not re.fullmatch('[a-f0-9]{64}', digest) or type(item.get('size')) is not int
                    or not 0 < item['size'] <= MAX_WHEEL):
                continue
            try:
                if requirement and not SpecifierSet(requirement).contains('.'.join(map(str, sys.version_info[:3]))):
                    continue
            except ValueError:
                continue
            found.append((parsed, Candidate(version, filename, url, digest, item['size'], channel)))
    if not found:
        raise EngineError('官方没有返回适合当前 Python 的可用版本。当前引擎保持不变。')
    candidate = max(found, key=lambda item: item[0])[1]
    return {'current': current_version(), 'candidate': candidate,
            'available': Version(candidate.version) > Version(current_version())}


def _remove_owned(path, root):
    """Delete only the exact verified staging/owned slot inside our engine root."""
    path, root = Path(path), root_path(root)
    resolved = path.resolve()
    if resolved.parent != root.resolve() or path.is_symlink() or path.is_junction():
        raise EngineError('清理目标不属于解析引擎目录，已停止。')
    if not path.name.startswith('.incoming-'):
        slot_path(path.name, root)
    for base, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            item = Path(base) / name
            if item.is_symlink() or item.is_junction():
                raise EngineError('版本目录包含文件链接，未执行清理。')
    shutil.rmtree(resolved)


def _extract(wheel, destination, expected):
    with zipfile.ZipFile(wheel) as archive:
        items = archive.infolist()
        if len(items) > 6000 or sum(item.file_size for item in items) > 128 * 1024 * 1024:
            raise EngineError('引擎安装包大小或文件数量异常，未使用。')
        metadata, members = [], []
        for item in items:
            parts = PurePosixPath(item.filename).parts
            if (not parts or item.filename.startswith('/') or '\\' in item.filename or ':' in item.filename
                    or '..' in parts or (item.external_attr >> 16) & 0o170000 == 0o120000
                    or not (parts[0] == 'yt_dlp' or re.fullmatch(r'yt_dlp-[A-Za-z0-9._+]+\.(?:dist-info|data)', parts[0]))):
                raise EngineError('引擎安装包包含不支持的路径，未使用。')
            # Wheel .data contains shell completions/man pages. They are not
            # needed in a Python overlay and must not be installed into PATH.
            if parts[0].endswith('.data'):
                continue
            members.append(item)
            if item.filename.endswith('.dist-info/METADATA'):
                metadata.append(archive.read(item))
        if len(metadata) != 1:
            raise EngineError('引擎安装包的版本信息不完整。')
        info = BytesParser().parsebytes(metadata[0])
        Version, _, _ = versions()
        if info['Name'].lower().replace('_', '-') != 'yt-dlp' or Version(info['Version']) != Version(expected):
            raise EngineError('下载包与选中的引擎版本不一致，未使用。')
        archive.extractall(destination, members)


def install_candidate(candidate, cancel, progress=lambda _: None, proxy='', root=None):
    # Hold the interprocess lock throughout staging so no second app can change
    # the plan underneath validation or prune a version this operation needs.
    with state_lock(root) as root:
        state = read_state(root)
        if state['pending'] is not None or state['trial'] is not None:
            raise EngineError('已有待启用版本，请先重新打开软件，或取消这次安排。')
        if shutil.disk_usage(root).free < 160 * 1024 * 1024:
            raise EngineError('项目所在磁盘空间不足，请至少留出 160 MB 后再更新。')
        work = root / ('.incoming-' + uuid.uuid4().hex)
        work.mkdir()
        try:
            check_cancel(cancel)
            # Re-check the supplied candidate; the UI never supplies executable
            # commands, arbitrary repositories, credentials or install scripts.
            if (urlsplit(candidate.url).scheme != 'https' or urlsplit(candidate.url).hostname != 'files.pythonhosted.org'
                    or not re.fullmatch('[a-f0-9]{64}', candidate.sha256) or not 0 < candidate.size <= MAX_WHEEL):
                raise EngineError('版本下载信息不可靠，请重新检查更新。')
            wheel = work / 'engine.whl'
            progress('正在下载官方解析引擎；现有环境保持。')
            digest, size, started = hashlib.sha256(), 0, time.monotonic()
            with _session(proxy, candidate.url) as session, session.get(candidate.url, stream=True, timeout=(10, 15), allow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise EngineError('官方下载未返回完整安装包，请稍后重试。')
                with wheel.open('xb') as stream:
                    for chunk in response.iter_content(65536):
                        check_cancel(cancel)
                        size += len(chunk)
                        if size > candidate.size or time.monotonic() - started > 120:
                            raise EngineError('下载大小或等待时间超过限制，已停止。')
                        digest.update(chunk)
                        stream.write(chunk)
            if size != candidate.size or digest.hexdigest() != candidate.sha256:
                raise EngineError('下载文件未通过完整性校验，未启用；请重新检查更新后再试。')
            check_cancel(cancel)
            package = work / 'package'
            package.mkdir()
            _extract(wheel, package, candidate.version)
            progress('下载已校验，正在用本机测试视频验证解析、下载及 MP4 转换…')
            check_cancel(cancel)
            result = probe(package, full=True)
            Version, _, _ = versions()
            if Version(result['version']) != Version(candidate.version) or not result.get('mp4'):
                raise EngineError('新版验证未确认正确版本或 MP4 输出，未启用。')
            check_cancel(cancel)
            slot = 'v-' + re.sub('[^A-Za-z0-9._-]', '_', candidate.version)[:80] + '-' + uuid.uuid4().hex[:12]
            (package / 'engine.json').write_text(json.dumps(dict(owner=OWNER, slot=slot, version=candidate.version,
                                                                 validated=True, sha256=candidate.sha256)), encoding='utf-8')
            target = root / slot
            package.rename(target)
            try:
                state.update(pending=slot, notice='新版验证通过，关闭并重新打开软件后启用；当前窗口仍使用原引擎。')
                write_state(state, root)
            except Exception:
                _remove_owned(target, root)
                raise
            # Keep active, previous and pending; only remove positively owned
            # obsolete slots. Unknown user files are never a cleanup target.
            keep = {state.get(key) for key in ('active', 'previous', 'pending')}
            for path in root.iterdir():
                try:
                    if path.name not in keep and path.name.startswith('v-') and path.is_dir() and not in_use(path.name, root):
                        _remove_owned(path, root)
                except (OSError, EngineError):
                    pass
            return result
        finally:
            if work.exists():
                _remove_owned(work, root)
