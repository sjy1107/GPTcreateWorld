"""Plain filename rules and publication that never overwrites an existing MP4."""
import os
import re
from pathlib import Path

NAMING_MODES = {"title_quality", "title"}


def safe_stem(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100].rstrip(" .")
    if not title:
        return "video"
    if title.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        title = "_" + title
    return title


def filename_stem(title, height=None, *, filename="", naming="title_quality"):
    if filename.strip():
        return safe_stem(re.sub(r"(?:\.mp4)+$", "", filename.strip(), flags=re.I))
    stem = safe_stem(re.sub(r"\.mp4$", "", title, flags=re.I))
    if naming == "title":
        return stem
    try:
        height = int(height)
    except (ValueError, TypeError, OverflowError):
        height = 0
    return f"{stem}_{height}p" if height > 0 else f"{stem}_原画质"


def publish_mp4(source: Path, folder: Path, stem: str) -> Path:
    for number in range(1, 10001):
        target = folder / f"{stem}{f' ({number})' if number > 1 else ''}.mp4"
        try:
            if os.name == "nt":
                # Windows rename fails if another writer already took this name.
                source.rename(target)
            else:
                os.link(source, target)
                source.unlink()
            return target
        except FileExistsError:
            continue
    raise FileExistsError("同名文件过多，请修改文件名后重试。")
