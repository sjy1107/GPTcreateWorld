"""Destination preflight and bounded, low-frequency runtime storage checks."""
from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MIN_FREE = 64 * 1024**2
FAT32_LIMIT = 2**32 - 1


class StorageError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class VolumeInfo:
    filesystem: str = ""
    max_file_bytes: int | None = None


def volume_info(folder: Path) -> VolumeInfo:
    if os.name != 'nt':
        return VolumeInfo()
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    volume_path = ctypes.create_unicode_buffer(32768)
    get_path = kernel.GetVolumePathNameW
    get_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_path.restype = wintypes.BOOL
    if not get_path(str(folder), volume_path, len(volume_path)):
        return VolumeInfo()
    name = ctypes.create_unicode_buffer(256)
    get_info = kernel.GetVolumeInformationW
    get_info.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                         ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD)
    get_info.restype = wintypes.BOOL
    if not get_info(volume_path.value, None, 0, None, None, None, name, len(name)):
        return VolumeInfo()
    return VolumeInfo(name.value, FAT32_LIMIT if name.value.upper() == 'FAT32' else None)


def preflight_destination(folder: Path, *, estimated_bytes=None, stems=()):
    """Test actual write, flush and rename before any media request."""
    folder = folder.expanduser().resolve()
    # FFmpeg and upstream tools do not consistently accept Windows long paths.
    if os.name == 'nt':
        candidates = [folder / '.shiying-partials' / ('f' * 64) / 'source.f999999.mp4.part',
                      *(folder / (stem + ' (10000).mp4') for stem in stems)]
        if any(len(str(path).encode('utf-16-le')) // 2 >= 260 for path in candidates):
            raise StorageError('path_too_long', '保存路径加上视频文件名后过长，请选择更靠近盘符的文件夹，或缩短文件名。')
    if folder.exists() and not folder.is_dir():
        raise StorageError('destination_unavailable', '保存位置是一个文件，请选择用于存放视频的文件夹。')
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise StorageError('destination_unavailable', '保存位置不是可用的文件夹，请重新选择。')
    original = renamed = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.shiying-write-', suffix='.tmp', dir=folder, delete=False) as probe:
            original = Path(probe.name)
            probe.write(b'shiying storage check\n')
            probe.flush()
            os.fsync(probe.fileno())
        renamed = original.with_suffix('.checked')
        original.rename(renamed)
    finally:
        for path in (original, renamed):
            if path is not None:
                path.unlink(missing_ok=True)
    volume = volume_info(folder)
    if volume.max_file_bytes and estimated_bytes and estimated_bytes > volume.max_file_bytes:
        raise StorageError('file_too_large', '预计视频素材超过此磁盘约 4 GB 的单文件限制（FAT32）。请改用 NTFS / exFAT 磁盘或选择较低画质；软件不会格式化磁盘。')
    return volume


class StorageGuard:
    CHECK_INTERVAL = 2

    def __init__(self, folder: Path, notify, volume=None):
        self.folder = folder
        status = folder.stat()
        self.identity = (status.st_dev, status.st_ino)
        self.volume = volume or volume_info(folder)
        self.notify = notify
        self.last_check = None

    def check(self, *, force=False, reserve=MIN_FREE, file=None, file_bytes=None):
        now = time.monotonic()
        if not force and self.last_check is not None and now - self.last_check < self.CHECK_INTERVAL:
            return
        self.last_check = now
        try:
            status = self.folder.stat()
            if (status.st_dev, status.st_ino) != self.identity or not self.folder.is_dir():
                raise StorageError('destination_unavailable', '保存位置已被移动或替换，请重新确认磁盘及文件夹。')
            free = shutil.disk_usage(self.folder).free
            size = Path(file).stat().st_size if file is not None and Path(file).exists() else file_bytes
        except OSError as exc:
            raise StorageError('destination_unavailable', '保存位置暂时无法读取，磁盘可能已断开或失去访问权限。') from exc
        self.notify('storage', {'free': free, 'filesystem': self.volume.filesystem})
        if self.volume.max_file_bytes and size and size >= self.volume.max_file_bytes - 1024**2:
            raise StorageError('file_too_large', '当前文件已接近此磁盘约 4 GB 的单文件限制，请换用 NTFS / exFAT 磁盘或降低画质后重试。')
        if free < max(MIN_FREE, reserve):
            raise StorageError('disk_low', '保存盘剩余空间不足，已停止当前及后续下载，避免继续占满磁盘。请释放空间后重试；合并和整理 MP4 也需要额外空间。')

    def before_processing(self, files):
        size = sum(path.stat().st_size for path in files if path.is_file())
        self.check(force=True, reserve=size + MIN_FREE)
