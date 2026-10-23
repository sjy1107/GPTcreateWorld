"""Remember only a chosen download directory; never persist credentials or URLs."""
import json
import os
import tempfile
from pathlib import Path
from .local_storage import replace_record

PREFERENCES = Path(__file__).resolve().parent.parent / ".local" / "preferences.json"
MAX_BYTES = 256 * 1024


def load_folder(default: Path) -> str:
    try:
        with PREFERENCES.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return str(default)
        value = json.loads(raw).get("folder")
        return value if isinstance(value, str) and value.strip() and len(value) <= 32768 and "\x00" not in value else str(default)
    except (OSError, ValueError, AttributeError, RecursionError):
        return str(default)


def save_folder(folder: Path):
    text = str(folder)
    if len(text) > 32768 or "\x00" in text:
        raise ValueError("保存位置过长或含无效字符。")
    PREFERENCES.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=PREFERENCES.parent,
                                         prefix=".preferences-", suffix=".tmp", delete=False) as target:
            temp_path = Path(target.name)
            json.dump({"folder": text}, target, ensure_ascii=False)
            target.flush()
            os.fsync(target.fileno())
        replace_record(temp_path, PREFERENCES)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
