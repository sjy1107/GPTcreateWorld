"""Broader static player discovery and real yt-dlp embed delegation contracts."""
import copy
import functools
import html
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp
from yt_dlp.extractor.common import InfoExtractor

from tests.integration_demo import FIXTURES, Handler, inspect, make_fixtures
from video_demo.core import (Cancelled, Network, PageYoutubeDL, Video, analyze,
                             discover_html, download, generic_retry_allowed, options)
from video_demo.site_compat import playback_url
from video_demo.web_discovery import MAX_ALTERNATIVES, MAX_CANDIDATES, MAX_LITERAL


class DiscoveryTests(unittest.TestCase):
    base = 'https://example.test/articles/page'

    def test_lazy_video_sources_and_iframes_are_found_and_placeholder_urls_ignored(self):
        content = '''<video title="Lazy"><source src="about:blank" data-src="../low.mp4">
        <source data-lazy-src="../high.mp4"></video>
        <iframe src="about:blank" data-original="https://player.test/embed/1"></iframe>
        <a href="/ordinary-page">do not crawl</a>'''
        found = discover_html(content, self.base)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].urls, ['https://example.test/low.mp4', 'https://example.test/high.mp4'])
        self.assertEqual(found[1].urls, ['https://player.test/embed/1'])

    def test_inline_srcdoc_uses_relative_base_and_has_depth_limit(self):
        inline = '<video src="/one.mp4"></video>'
        wrapped = f'<iframe srcdoc="{html.escape(inline, quote=True)}"></iframe>'
        self.assertEqual(discover_html(wrapped, self.base)[0].urls, ['https://example.test/one.mp4'])
        for _ in range(2):
            wrapped = f'<iframe srcdoc="{html.escape(wrapped, quote=True)}"></iframe>'
        self.assertEqual(discover_html(wrapped, self.base), [])

    def test_json_ld_arrays_graphs_and_video_objects_are_separate(self):
        data = {'@graph': [{'@type': ['CreativeWork', 'VideoObject'], 'name': 'One',
                           'contentUrl': '/one.mp4', 'embedUrl': 'https://player.test/embed/one'},
                          {'@type': 'https://schema.org/VideoObject', 'name': 'Two', 'contentUrl': '/two.m3u8'},
                          {'@type': 'ImageObject', 'contentUrl': '/not-video.mp4'}]}
        found = discover_html('<script type="application/ld+json">' + json.dumps(data) + '</script>', self.base)
        self.assertEqual([item.title for item in found], ['One', 'Two'])
        self.assertEqual(len(found[0].urls), 2)

    def test_metadata_for_existing_video_does_not_create_duplicate_embed(self):
        content = '<video src="/one.mp4"></video><script type="application/ld+json">' + json.dumps(
            {'@type': 'VideoObject', 'contentUrl': '/one.mp4', 'embedUrl': '/embed'}) + '</script>'
        found = discover_html(content, self.base)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].urls, ['https://example.test/one.mp4', 'https://example.test/embed'])

    def test_multiple_jwplayer_literals_and_playlists_keep_video_boundaries(self):
        content = '''<script>
        jwplayer("first").setup({playlist: [{title:'One', sources:[{file:'/one.mp4'}, {file:'/one.webm'}]},
                                            {title:'Two', file:'/two.m3u8'}]});
        jwplayer("second").load([{title:'Three', file:'/three.mp4'}]);
        </script>'''
        found = discover_html(content, self.base)
        self.assertEqual([item.title for item in found], ['One', 'Two', 'Three'])
        self.assertEqual(len(found[0].urls), 2)

    def test_videojs_custom_element_and_data_setup_are_supported(self):
        setup = html.escape(json.dumps({'sources': [{'src': '/one.mp4'}, {'src': '/one.webm'}]}), quote=True)
        found = discover_html(f'<video-js title="One" data-setup="{setup}"></video-js>', self.base)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0].urls), 2)

    def test_remote_scripts_do_not_consume_inline_budget_and_bad_blocks_are_skipped(self):
        content = '<script src="/lib.js"></script>' * 60 + '<script type="application/ld+json">INVALID</script>'
        content += '<script>' + 'x' * (MAX_LITERAL + 1) + '</script>'
        content += '<script type="application/ld+json">{"@type":"VideoObject","contentUrl":"/ok.mp4"}</script>'
        self.assertEqual(discover_html(content, self.base)[0].urls, ['https://example.test/ok.mp4'])

    def test_no_script_execution_credentials_or_unsupported_scheme(self):
        content = '''<video src="blob:https://example.test/id"></video>
          <iframe data-src="javascript:alert(1)"></iframe><video src="file:///C:/secret"></video>
          <video src="https://user:pass@example.test/video.mp4"></video>
          <script>jwplayer('p').setup(loadSecret()); jwplayer('other').setup({file: videoUrl});</script>
          <a href="/normal">normal</a>'''
        self.assertEqual(discover_html(content, self.base), [])

    def test_candidate_and_alternative_limits_are_enforced(self):
        content = '<video>' + ''.join(f'<source src="/{i}.mp4">' for i in range(30)) + '</video>'
        content += ''.join(f'<video src="/separate{i}.mp4"></video>' for i in range(100))
        found = discover_html(content, self.base)
        self.assertEqual(len(found), MAX_CANDIDATES)
        self.assertEqual(len(found[0].urls), MAX_ALTERNATIVES)

    def test_invalid_base_cannot_replace_valid_page_url(self):
        found = discover_html('<base href="javascript:alert(1)"><video src="/valid.mp4"></video>', self.base)
        self.assertEqual(found[0].urls, ['https://example.test/valid.mp4'])


class CompatibilityRoutingTests(unittest.TestCase):
    def test_bilibili_bvid_player_normalizes_and_keeps_explicit_part(self):
        link = 'https://player.bilibili.com/player.html?bvid=BV1jjJk6cE5W&page=2&cid=123&autoplay=1'
        self.assertEqual(playback_url(link), 'https://www.bilibili.com/video/BV1jjJk6cE5W/?p=2')
        self.assertEqual(playback_url('https://player.bilibili.com/player.html?bvid=BV1jjJk6cE5W'),
                         'https://www.bilibili.com/video/BV1jjJk6cE5W/?p=1')
        for suffix in ('bvid=invalid', 'bvid=BV1jjJk6cE5W&cid=123', 'bvid=BV1jjJk6cE5W&page=0',
                       'bvid=BV1jjJk6cE5W&page=1&page=2', 'bvid=BV1jjJk6cE5W&bvid=BV1jjJk6cE5W'):
            original = 'https://player.bilibili.com/player.html?' + suffix
            self.assertEqual(playback_url(original), original)
        evil = 'https://player.bilibili.com.evil.test/player.html?bvid=BV1jjJk6cE5W'
        self.assertEqual(playback_url(evil), evil)

    def test_generic_retry_is_for_parser_failures_not_access_or_network(self):
        for error in ('Unsupported URL: https://example.test', 'Unable to extract player data', 'No video formats found'):
            self.assertTrue(generic_retry_allowed([RuntimeError(error)]))
        for error in ('HTTP Error 403', 'HTTP Error 429', 'HTTP Error 503', 'DRM protected', 'Login required',
                      'Fresh cookies are needed', 'This video is protected by a password', 'Connection timed out'):
            self.assertFalse(generic_retry_allowed([RuntimeError(error)]), error)

    def test_unsupported_site_can_use_forced_generic_once_and_keep_main_error_priority(self):
        info = {'id': 'sample', 'title': 'Sample', 'extractor_key': 'Generic',
                'formats': [{'url': 'https://media.test/one.mp4', 'vcodec': 'h264', 'height': 360}]}
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', side_effect=[RuntimeError('Unable to extract player'), info]) as extract, \
             patch('video_demo.core.page_candidates', return_value=[]):
            videos = analyze('https://example.test/watch', threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 1)
        self.assertEqual(extract.call_count, 2)
        self.assertTrue(extract.call_args.kwargs['force_generic_extractor'])

    def test_same_provider_video_with_rotated_urls_is_not_added_twice(self):
        main = {'id': 'same', 'title': 'Same', 'extractor_key': 'FixtureProvider',
                'formats': [{'url': 'https://media.test/one.mp4?token=old', 'vcodec': 'h264'}]}
        extra = copy.deepcopy(main)
        extra['formats'][0]['url'] = 'https://media.test/one.mp4?token=new'
        from video_demo.web_discovery import Candidate
        # Root Generic delegates to the provider, which changes URL on reparse.
        def extract(ydl, url, **kwargs):
            ydl.root_extractor = 'Generic'
            return copy.deepcopy(main if url.endswith('/page') else extra)
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', extract), \
             patch('video_demo.core.page_candidates', return_value=[Candidate(['https://player.test/embed'])]):
            videos = analyze('https://example.test/page', threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 1)

    def test_default_headers_follow_upstream_instead_of_forcing_old_browser(self):
        opts = options(lambda *_: None, threading.Event(), 'https://parent.test')
        self.assertEqual(opts['http_headers'], {'Referer': 'https://parent.test'})


class RealPageCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_fixtures()
        cls.pages, cls.requests = {}, []
        class PageHandler(Handler):
            def do_GET(self):
                cls.requests.append((self.path, self.headers.get('Referer')))
                if self.path in cls.pages:
                    body = cls.pages[self.path].encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith('/guarded.mp4'):
                    if self.headers.get('Referer') != cls.base + '/lazy.html':
                        self.send_error(403)
                        return
                    self.path = '/first.mp4'
                return super().do_GET()
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(PageHandler, directory=str(FIXTURES)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.pages['/lazy.html'] = '<html><title>Lazy player</title><video data-src="/guarded.mp4"></video></html>'
        cls.pages['/json.html'] = '<html><title>Multiple videos</title><script type="application/ld+json">' + json.dumps([
            {'@type': 'VideoObject', 'name': 'One', 'contentUrl': cls.base + '/first.mp4'},
            {'@type': 'VideoObject', 'name': 'Two', 'contentUrl': cls.base + '/second.webm'}]) + '</script></html>'
        cls.pages['/jw.html'] = '''<html><title>Players</title><script>
            jwplayer('a').setup({file:'/first.mp4', title:'First'});
            jwplayer('b').setup({playlist:[{file:'/linked.mp4',title:'Second'}, {file:'/second.webm',title:'Third'}]});
            </script></html>'''
        cls.pages['/mixed.html'] = f'<html><title>Mixed</title><iframe src="{cls.base}/fixture-embed/1"></iframe><video src="/linked.mp4"></video></html>'
        cls.pages['/empty.html'] = '<html><title>No video</title></html>'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_lazy_media_download_keeps_page_referer_and_decodes_as_mp4(self):
        videos = analyze(self.base + '/lazy.html', threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 1)
        with tempfile.TemporaryDirectory() as folder:
            path = download(videos[0], None, Path(folder), threading.Event(), lambda *_: None, Network(keep_progress=True))
            self.assertEqual(path.suffix, '.mp4')
            inspect(path)
        guarded = [referer for path, referer in self.requests if path.startswith('/guarded.mp4')]
        self.assertTrue(guarded)
        self.assertTrue(all(value == self.base + '/lazy.html' for value in guarded))

    def test_all_json_ld_videos_are_resolved_and_downloaded(self):
        videos = analyze(self.base + '/json.html', threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 2)
        with tempfile.TemporaryDirectory() as folder:
            for item in videos:
                path = download(item, None, Path(folder), threading.Event(), lambda *_: None)
                self.assertEqual(path.suffix, '.mp4')
                inspect(path)

    def test_multiple_jwplayer_instances_and_playlist_entries_are_found(self):
        videos = analyze(self.base + '/jw.html', threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 3)

    def test_known_html5_source_does_not_hide_new_alternative_formats(self):
        primary = {'id': 'existing', 'title': 'Existing', 'extractor_key': 'HTML5MediaEmbed',
                   'formats': [{'format_id': 'original', 'url': self.base + '/first.mp4', 'ext': 'mp4',
                                'vcodec': 'h264', 'acodec': 'aac', 'height': 360}]}
        from video_demo.web_discovery import Candidate
        original = yt_dlp.YoutubeDL.extract_info
        def extract(ydl, url, **kwargs):
            if url == self.base + '/live.m3u8':
                return {**copy.deepcopy(primary), 'is_live': True}
            return copy.deepcopy(primary) if url == self.base + '/test.html' else original(ydl, url, **kwargs)
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', extract), \
             patch('video_demo.core.page_candidates', return_value=[Candidate([self.base + '/first.mp4', self.base + '/second.webm', self.base + '/live.m3u8'])]):
            videos = analyze(self.base + '/test.html', threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 1)
        self.assertEqual(len(videos[0].info['formats']), 2)
        self.assertEqual(videos[0].info['id'], 'existing')
        self.assertEqual(primary['formats'][0]['format_id'], 'original')

    def test_transparent_provider_result_does_not_hide_other_html5_videos(self):
        base = self.base
        class FixtureEmbedIE(InfoExtractor):
            _VALID_URL = r'http://127\.0\.0\.1:\d+/fixture-embed/(?P<id>\d+)'
            _EMBED_REGEX = [r'<iframe[^>]+src="(?P<url>http://127\.0\.0\.1:\d+/fixture-embed/\d+)"']
            def _real_extract(self, url):
                return {'id': self._match_id(url), 'title': 'Embedded provider', 'url': base + '/first.mp4',
                        'ext': 'mp4', 'vcodec': 'h264', 'acodec': 'aac'}
        initialize = PageYoutubeDL.__init__
        def init(ydl, *args, **kwargs):
            initialize(ydl, *args, **kwargs)
            ydl.add_info_extractor(FixtureEmbedIE(ydl))
        with patch.object(PageYoutubeDL, '__init__', init):
            videos = analyze(self.base + '/mixed.html', threading.Event(), lambda *_: None)
        self.assertEqual(len(videos), 2)
        self.assertIn('FixtureEmbed', {item.info['extractor_key'] for item in videos})
        self.assertTrue(any('/linked.mp4' in f.get('url', '') for item in videos for f in item.info.get('formats', [])))

    def test_already_generic_empty_page_does_not_repeat_same_parser(self):
        events = []
        with self.assertRaises(Exception):
            analyze(self.base + '/empty.html', threading.Event(), lambda *event: events.append(event))
        self.assertFalse(any('通用播放器规则' in data.get('text', '') for kind, data in events))

    def test_cancel_stops_before_supplemental_scan(self):
        cancel = threading.Event()
        def notify(kind, data):
            if kind == 'log' and '补充查找' in data.get('text', ''):
                cancel.set()
        with self.assertRaises(Cancelled):
            with patch('video_demo.core.page_candidates', side_effect=AssertionError('scan ran after cancellation')):
                analyze(self.base + '/json.html', cancel, notify)


if __name__ == '__main__':
    unittest.main()
