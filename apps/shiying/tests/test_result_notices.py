"""Factual per-video extraction notices, collection context and Qt presentation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import unittest
from unittest.mock import patch

import yt_dlp
from yt_dlp.extractor.common import InfoExtractor

from tests.test_workflow_additions import WindowTestCase
from tests.test_stability import sample
from video_demo.core import PageYoutubeDL, Video, analyze, options
from video_demo.result_notices import NoticeCapture, classify_message, notices_for
from video_demo.result_notice_dialog import ResultNoticeDialog


class ResultNoticeTests(unittest.TestCase):
    def test_public_and_successful_parse_do_not_claim_completeness(self):
        for info in ({}, {'availability': 'public'}, {'availability': 'unlisted'}, {'media_type': 'trailer'}):
            notes = notices_for(info)
            self.assertEqual([note.code for note in notes], ['unknown'])
            self.assertFalse(notes[0].warning)

    def test_explicit_preview_and_access_conditions_are_explained(self):
        self.assertEqual(notices_for({'is_preview': True})[0].code, 'preview')
        for value in ('private', 'premium_only', 'subscriber_only', 'needs_auth'):
            self.assertIn('access', {note.code for note in notices_for({'availability': value})})
        self.assertIsNone(classify_message('See https://example.test/only_preview.mp4'))

    def test_capture_matches_identity_and_does_not_leak_between_videos(self):
        capture = NoticeCapture()
        capture.context = ('bilibili', 'BVexample')
        capture.record('Format(s) 1080P are missing; you have to become a premium member')
        one = {'extractor_key': 'BiliBili', 'id': 'BVexample_p2'}
        other = {'extractor_key': 'BiliBili', 'id': 'Other'}
        capture.apply(one)
        capture.apply(other)
        self.assertIn('missing_quality', {note.code for note in notices_for(one)})
        self.assertNotIn('missing_quality', {note.code for note in notices_for(other)})

    def test_malformed_optional_notes_do_not_crash(self):
        for value in (None, 123, {'code': 'preview'}, ['bad'], [{'code': 'preview'}]):
            self.assertEqual(notices_for({'_shiying_notices': value})[0].code, 'unknown')
        for value in (None, [], {}, 123):
            self.assertEqual(notices_for({'availability': value})[0].code, 'unknown')

    def test_real_playlist_extractor_warnings_stay_on_their_own_video(self):
        class NoticeItemIE(InfoExtractor):
            _VALID_URL = r'https://notice\.invalid/item/(?P<id>\w+)'
            def _real_extract(self, url):
                identity = self._match_id(url)
                if identity == 'preview':
                    self.report_warning('only the preview will be extracted', video_id=identity)
                    self.to_screen('Format(s) 1080P are missing; premium member required')
                return {'id': identity, 'title': identity, 'url': 'https://media.invalid/' + identity + '.mp4',
                        'ext': 'mp4', 'vcodec': 'h264', 'acodec': 'aac'}
        class NoticeListIE(InfoExtractor):
            _VALID_URL = r'https://notice\.invalid/(?P<id>list)'
            def _real_extract(self, url):
                return self.playlist_result([self.url_result('https://notice.invalid/item/' + identity, NoticeItemIE)
                                             for identity in ('preview', 'normal')], 'list')
        opts = options(lambda *_: None, threading.Event())
        with PageYoutubeDL(opts) as ydl:
            ydl.add_info_extractor(NoticeListIE(ydl))
            ydl.add_info_extractor(NoticeItemIE(ydl))
            result = ydl.extract_info('https://notice.invalid/list', download=False, ie_key='NoticeList')
        entries = list(result['entries'])
        first, second = [notices_for(entry) for entry in entries]
        self.assertIn('preview', {note.code for note in first})
        self.assertIn('missing_quality', {note.code for note in first})
        self.assertEqual([note.code for note in second], ['unknown'])

    def test_partial_playlist_is_not_silently_reported_as_all_videos(self):
        item = sample()
        result = {'_type': 'playlist', 'entries': [item.info, None]}
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', return_value=result), \
             patch('video_demo.core.page_candidates', return_value=[]):
            videos = analyze('https://example.test/list', threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 1)
        self.assertIn('1 个条目未能解析', videos[0].info['_shiying_collection_notice'])
        self.assertIn('当前视频是否完整需分别确认', videos[0].info['_shiying_collection_notice'])


class ResultNoticeUiTests(WindowTestCase):
    def test_download_success_keeps_preview_notice_and_plain_text_details(self):
        item = sample()
        item.info['is_preview'] = True
        item.title = '<b>plain title</b>'
        self.window.populate([item])
        self.assertIn('试看', self.window.table.item(0, 5).text())
        self.window.handle_event('row', {'row': 0, 'state': '已完成'})
        self.assertEqual(self.window.table.item(0, 4).text(), '已完成')
        self.assertIn('试看', self.window.table.item(0, 5).text())
        dialog = ResultNoticeDialog([item], parent=self.window)
        self.assertIn('<b>plain title</b>', dialog.content.toPlainText())
        self.assertIn('不代表', dialog.content.toPlainText())
        dialog.deleteLater()

    def test_result_notice_button_and_unknown_are_available_without_auto_action(self):
        self.window.populate([sample()])
        self.assertTrue(self.window.notice_button.isEnabled())
        self.assertEqual(self.window.table.item(0, 5).text(), '完整性未确认')
        with patch.object(self.window, 'show_result_notices') as opened:
            self.window.notice_button.click()
            opened.assert_called_once()


if __name__ == '__main__':
    unittest.main()
