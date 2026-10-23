"""Bounded static discovery of embedded players and literal media configuration.

Read data only: no JavaScript evaluation, browser access or ordinary-link crawl.
The discovered URLs are still resolved/validated by yt-dlp before downloading.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from yt_dlp.utils import js_to_json

from .url_tools import validate_url

MAX_CANDIDATES = 50
MAX_ALTERNATIVES = 8
MAX_LITERAL = 128 * 1024
MAX_SCRIPTS = 32
LAZY_ATTRIBUTES = ("src", "data-src", "data-lazy-src", "data-original", "data-url")


@dataclass
class Candidate:
    urls: list[str]
    title: str = ""


def _json(value, *, javascript=False):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_LITERAL:
        return None
    try:
        value = value.lstrip()
        if value[0] not in "[{":
            return None
        if javascript:
            value = js_to_json(value)
        return json.JSONDecoder().raw_decode(value)[0]
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None


def discover_html(html, base_url: str) -> list[Candidate]:
    candidates = []

    def scan(document, page_base, depth=0):
        if depth > 2 or len(candidates) >= MAX_CANDIDATES:
            return
        soup = BeautifulSoup(document, "html.parser")
        base = soup.find("base", href=True)
        if base:
            try:
                page_base = validate_url(urljoin(page_base, base["href"]))
            except ValueError:
                pass

        def add(values, title=""):
            if len(candidates) >= MAX_CANDIDATES:
                return
            urls = []
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    continue
                value = value.strip()
                # blob/data/JavaScript are not remotely downloadable URLs.
                if re.match(r"(?i)^(?:blob|data|javascript|file):", value):
                    continue
                try:
                    url = validate_url(urljoin(page_base, value))
                except ValueError:
                    continue
                if url not in urls and url != page_base:
                    urls.append(url)
                if len(urls) >= MAX_ALTERNATIVES:
                    break
            if urls:
                owner = next((item for item in candidates if set(item.urls).intersection(urls)), None)
                if owner:
                    owner.urls = list(dict.fromkeys(owner.urls + urls))[:MAX_ALTERNATIVES]
                    if not owner.title and isinstance(title, str):
                        owner.title = title
                    return
                candidates.append(Candidate(urls, str(title or "")))

        def config(data, level=0):
            if level > 12 or len(candidates) >= MAX_CANDIDATES:
                return
            if isinstance(data, list):
                # An explicit JW Player playlist contains independent videos.
                for item in data[:MAX_CANDIDATES]:
                    config(item, level + 1)
            elif isinstance(data, dict):
                playlist = data.get("playlist")
                if isinstance(playlist, (list, dict)):
                    config(playlist, level + 1)
                values = [data.get("file"), data.get("src")]
                sources = data.get("sources") or []
                if isinstance(sources, dict):
                    sources = [sources]
                if isinstance(sources, list):
                    values.extend(item.get("file") or item.get("src") if isinstance(item, dict) else item
                                  for item in sources[:MAX_ALTERNATIVES])
                # js_to_json represents unresolved identifiers as strings.
                # Do not mistake a variable name for a relative media URL.
                values = [value for value in values if isinstance(value, str)
                          and ('/' in value or re.search(r'\.(?:mp4|m3u8|mpd|webm|mov)(?:[?#]|$)', value, re.I))]
                add(values, data.get("title") or data.get("name"))

        for tag in soup.find_all(["video", "video-js"]):
            values = [tag.get(attr) for attr in LAZY_ATTRIBUTES]
            values.extend(source.get(attr) for source in tag.find_all("source") for attr in LAZY_ATTRIBUTES)
            setup = _json(tag.get("data-setup"))
            if isinstance(setup, dict):
                sources = setup.get("sources") or []
                if isinstance(sources, list):
                    values.extend(item.get("src") for item in sources[:MAX_ALTERNATIVES] if isinstance(item, dict))
            add(values, tag.get("title") or tag.get("aria-label"))
        for tag in soup.find_all(["iframe", "embed", "object", "a", "meta"]):
            if tag.name == "iframe" and isinstance(tag.get("srcdoc"), str):
                inline = tag["srcdoc"]
                if len(inline) <= MAX_LITERAL:
                    scan(inline, page_base, depth + 1)
            values = [tag.get(attr) for attr in LAZY_ATTRIBUTES] + [tag.get("data"), tag.get("href")]
            if tag.name == "meta":
                name = (tag.get("property") or tag.get("name") or "").lower()
                if name not in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player", "twitter:player:stream"):
                    continue
                values = [tag.get("content")]
            if tag.name == "a":
                values = [value for value in values if isinstance(value, str)
                          and re.search(r"\.(mp4|m3u8|mpd|webm|mov)(?:[?#]|$)", value, re.I)]
            add(values, tag.get("title"))

        nodes = 0
        def video_objects(data, level=0):
            nonlocal nodes
            nodes += 1
            if level > 12 or nodes > 2000 or len(candidates) >= MAX_CANDIDATES:
                return
            if isinstance(data, list):
                for item in data[:MAX_CANDIDATES]:
                    video_objects(item, level + 1)
            elif isinstance(data, dict):
                types = data.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                if isinstance(types, list) and any(str(item).rstrip('/').rsplit('/', 1)[-1] == "VideoObject" for item in types):
                    values = []
                    for field in ("contentUrl", "embedUrl"):
                        value = data.get(field)
                        values.extend(value if isinstance(value, list) else [value])
                    add(values, data.get("name"))
                for value in list(data.values())[:100]:
                    if isinstance(value, (list, dict)):
                        video_objects(value, level + 1)

        literal_count = 0
        script_count = 0
        for tag in soup.find_all("script"):
            if tag.get("src"):
                continue
            source = tag.string or tag.get_text()
            if not source.strip() or len(source) > MAX_LITERAL:
                continue
            script_count += 1
            if script_count > MAX_SCRIPTS:
                break
            if tag.get("type", "").lower().split(';')[0].strip() == "application/ld+json":
                video_objects(_json(source))
                continue
            # Literal setup/load blocks only. Variables, functions and remote
            # scripts are never executed or fetched by this supplemental scan.
            pattern = r"jwplayer\s*\([^)]{0,200}\)\s*\.\s*(?:setup|load)\s*\("
            for match in re.finditer(pattern, source):
                if literal_count >= 16:
                    break
                literal_count += 1
                config(_json(source[match.end():], javascript=True))

    scan(html, base_url)
    return candidates
