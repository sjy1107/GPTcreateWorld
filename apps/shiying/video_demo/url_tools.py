"""HTTP URL validation shared by page discovery and the download engine."""
from urllib.parse import urlsplit, urlunsplit


def validate_url(value: str) -> str:
    value = value.strip()
    try:
        parts = urlsplit(value)
        if any(char.isspace() or ord(char) < 32 for char in value) or parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError
        if parts.username or parts.password:
            raise ValueError
        parts.port
    except ValueError:
        raise ValueError("请输入完整的 http:// 或 https:// 网址（不能包含账号密码）。") from None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
