"""Small, URL-free facts about extraction; never infer completeness from success."""
from dataclasses import dataclass
import re

from .errors import redact_detail


@dataclass(frozen=True)
class Notice:
    code: str
    title: str
    detail: str
    warning: bool = True


def classify_message(message):
    text = redact_detail(str(message))[:1500]
    if re.search(r'only (?:the )?preview|only.{0,40}preview will be extracted|preview.only|只能试看|仅.{0,8}试看|only.{0,20}trial', text, re.I):
        return Notice('preview', '可能只有试看内容',
                      '网站提示当前只能取得试看或预览部分。即使下载完成，也不代表已保存完整视频。\n网站提示：' + text)
    if re.search(r'format\(s\).{0,250}(?:missing|unavailable)|some.{0,30}formats.{0,100}(?:missing|skipped|unavailable)|部分画质.{0,20}(?:缺失|不可用)|缺少.{0,12}画质', text, re.I):
        return Notice('missing_quality', '部分画质未取得',
                      '只会列出本次实际取得的画质。网站可能要求其他权限，或当前解析方式没有取得全部格式。\n网站提示：' + text)
    return None


def notices_for(info):
    notes = []
    records = info.get('_shiying_notices')
    for item in records[:12] if isinstance(records, list) else []:
        if (isinstance(item, dict) and item.get('code') in ('preview', 'missing_quality')
                and isinstance(item.get('title'), str) and isinstance(item.get('detail'), str)):
            notes.append(Notice(item['code'], item['title'][:80], redact_detail(item['detail'])[:2000]))
    if info.get('is_preview') is True or info.get('is_trial') is True:
        notes.append(Notice('preview', '可能只有试看内容', '来源明确标记这是试看或预览版本；下载完成不代表完整视频。'))
    availability = info.get('availability')
    labels = {'private': '私密内容', 'premium_only': '付费或会员内容',
              'subscriber_only': '订阅内容', 'needs_auth': '需要账号访问'}
    if isinstance(availability, str) and availability in labels:
        notes.append(Notice('access', '网站设置了观看条件',
                            f'网站将此视频标记为“{labels[availability]}”。找到地址不代表拥有全部观看权限；请在原网页确认当前账号可观看的范围。'))
    unique = {note.code: note for note in notes}
    # Public/unlisted and a successful parse are not evidence of full content.
    if 'preview' not in unique:
        unique['unknown'] = Notice('unknown', '完整性未确认',
                                  '已找到可下载的资源，但网站未提供足够信息来证明内容完整。下载完成表示这些资源已保存并通过基本媒体检查；如有疑问，请与原网页的时长、内容对照。', False)
    return tuple(unique.values())


def brief(info):
    notes = notices_for(info)
    warnings = [note for note in notes if note.warning]
    return '；'.join(note.title for note in warnings) if warnings else '完整性未确认'


class NoticeCapture:
    def __init__(self):
        self.context = None
        self.records = []

    def record(self, message):
        note = classify_message(message)
        if note and self.context and len(self.records) < 100:
            self.records.append((self.context, note))

    def apply(self, info):
        extractor = str(info.get('extractor_key') or '').lower()
        identity = str(info.get('id') or '')
        notes = info.get('_shiying_notices')
        notes = list(notes) if isinstance(notes, list) else []
        for (site, video_id), note in self.records:
            # Parts can share a parent ID (e.g. BV..._p2). Never match substrings
            # arbitrarily or attach one video's warning to an unrelated entry.
            if site == extractor and video_id and (identity == video_id or identity.startswith(video_id + '_')):
                notes.append(dict(code=note.code, title=note.title, detail=note.detail))
        info['_shiying_notices'] = list({item['code']: item for item in notes if isinstance(item, dict) and 'code' in item}.values())[:12]
