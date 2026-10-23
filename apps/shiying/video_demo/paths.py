"""Separate writable portable data from bundled, read-only runtime files."""
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile


def frozen():
    return bool(getattr(sys, 'frozen', False))


def app_root():
    if not frozen():
        return Path(__file__).resolve().parent.parent
    directory = Path(sys.executable).resolve().parent
    if directory.name == '_internal' and (directory.parent / 'portable.flag').is_file():
        directory = directory.parent
    return directory


def data_root():
    return app_root() / ('data' if frozen() else '.local')


def resource_root():
    return Path(getattr(sys, '_MEIPASS', app_root()))


def tool_path(name):
    path = app_root() / 'tools' / name
    return path if path.is_file() else None


def encode_path(value):
    text = str(value)
    if frozen():
        try:
            relative = Path(text).resolve().relative_to(app_root().resolve())
            return 'portable:/' + relative.as_posix()
        except (OSError, ValueError):
            pass
    return text


def decode_path(value):
    if not isinstance(value, str):
        raise ValueError('保存位置记录无效。')
    if not value.startswith('portable:/'):
        return value
    relative = PurePosixPath(value[len('portable:/'):])
    if (not frozen() or relative.is_absolute() or any(part in ('..', '') or ':' in part or '\\' in part for part in relative.parts)):
        raise ValueError('便携保存位置记录无效。')
    path = app_root().joinpath(*relative.parts).resolve()
    if not path.is_relative_to(app_root().resolve()):
        raise ValueError('便携保存位置超出软件文件夹。')
    return str(path)


def prepare_portable():
    if not frozen():
        return
    root = data_root()
    if root.is_symlink() or root.is_junction():
        raise OSError('data 文件夹不能是链接，请重新解压到普通可写文件夹。')
    root.mkdir(exist_ok=True)
    # Verify the actual write operation; do not ask users to run as administrator.
    with tempfile.NamedTemporaryFile(dir=root, prefix='.write-', delete=True) as handle:
        handle.write(b'portable')
        handle.flush()
    temporary = root / 'temp'
    if temporary.is_symlink() or temporary.is_junction():
        raise OSError('临时目录不能是链接。')
    temporary.mkdir(exist_ok=True)
    tempfile.tempdir = str(temporary)
    os.environ['TEMP'] = os.environ['TMP'] = str(temporary)
