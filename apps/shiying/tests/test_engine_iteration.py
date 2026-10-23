"""Quality-first MP4 policy, actual local retry traffic and adapter contracts."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import functools
import http.server
import io
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import yt_dlp
from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError, CertificateVerifyError

from tests.integration_demo import FIXTURES, Handler, inspect, make_fixtures
from tests.test_workflow_additions import WindowTestCase
from video_demo.core import Cancelled, Network, Video, analyze, download, options
from video_demo.format_policy import MP4_FORMAT_SORT, format_selector
from video_demo.network_retry import RetryController, RetryYoutubeDL, retry_after
from video_demo.site_compat import ADAPTERS, adapter_for, apply_download_compat, fallback_for
from video_demo.task_control import ItemControl
from video_demo.transfer import estimate_video_bytes


def fmt(identity, height=720, codec="h264", audio="none", fps=30, **kwargs):
    return dict(format_id=identity, url=f"https://media.test/{identity}", ext="mp4",
                vcodec=codec, acodec=audio, height=height, width=height * 16 // 9,
                fps=fps, **kwargs)


def select(formats, height=None):
    info = dict(id="fixture", title="Fixture", formats=copy.deepcopy(formats))
    with yt_dlp.YoutubeDL(dict(quiet=True, no_warnings=True, cachedir=False,
                              check_formats=False, format=format_selector(height),
                              format_sort=list(MP4_FORMAT_SORT))) as ydl:
        return ydl.process_ie_result(info, download=False)


def http_error(status, headers=None):
    return HTTPError(Response(io.BytesIO(), "https://media.test/video", headers or {}, status=status))


class FormatPolicyTests(unittest.TestCase):
    def test_equivalent_video_and_audio_prefer_copyable_codecs(self):
        formats = [fmt("h264"), fmt("av1", codec="av01"),
                   fmt("aac", height=0, codec="none", audio="aac", filesize=100),
                   fmt("opus", height=0, codec="none", audio="opus", filesize=200)]
        result = select(formats)
        self.assertEqual([f['format_id'] for f in result['requested_formats']], ['h264', 'aac'])

    def test_combined_mp4_is_not_excluded_by_video_only_branch(self):
        result = select([fmt('combined', audio='aac'), fmt('av1', codec='av01'),
                         fmt('audio', height=0, codec='none', audio='aac')])
        self.assertEqual(result['format_id'], 'combined')

    def test_codec_preference_does_not_reduce_resolution_or_frame_rate(self):
        for higher in (fmt('higher', height=1080, codec='av01'),
                       fmt('higher', codec='av01', fps=60)):
            with self.subTest(higher=higher):
                result = select([fmt('low', audio='aac'), higher,
                                 fmt('audio', height=0, codec='none', audio='aac')])
                self.assertEqual(result['requested_formats'][0]['format_id'], 'higher')

    def test_height_ceiling_and_non_mp4_fallback_remain_available(self):
        result = select([fmt('720', codec='vp9', audio='opus'),
                         fmt('1080', height=1080, audio='aac')], 720)
        self.assertEqual(result['format_id'], '720')
        self.assertEqual(result['height'], 720)

    def test_audio_channels_and_language_preference_precede_codec(self):
        formats = [fmt('picture'),
                   fmt('aac', height=0, codec='none', audio='aac', audio_channels=2, language_preference=0),
                   fmt('surround', height=0, codec='none', audio='opus', audio_channels=6, language_preference=0)]
        self.assertEqual(select(formats)['requested_formats'][1]['format_id'], 'surround')
        formats[-1]['language_preference'] = -10
        self.assertEqual(select(formats)['requested_formats'][1]['format_id'], 'aac')

    def test_estimate_resorts_old_results_with_download_policy_without_network(self):
        info = dict(id='old', title='old', extractor_key='Fixture', formats=[
            fmt('combined', audio='aac', filesize=1234),
            fmt('av1', codec='av01', filesize=900),
            fmt('audio', height=0, codec='none', audio='opus', filesize=100)])
        item = Video.from_info(info, 'https://example.test/video')
        original = copy.deepcopy(info)
        with patch.object(yt_dlp.YoutubeDL, 'urlopen', side_effect=AssertionError('estimate went online')):
            self.assertEqual(estimate_video_bytes(item, 720), 1234)
        self.assertEqual(info, original)


class RetryPolicyTests(unittest.TestCase):
    def test_retry_after_accepts_seconds_dates_and_rejects_bad_values(self):
        self.assertEqual(retry_after({'Retry-After': '17'}), 17)
        self.assertEqual(retry_after({'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'}, now=1445412470), 10)
        self.assertEqual(retry_after({'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'}, now=1445412600), 0)
        for value in ('-1', 'nan', 'garbage', '3' * 129, ''):
            self.assertIsNone(retry_after({'Retry-After': value}))

    def test_backoff_uses_server_delay_and_never_adds_another_sleep(self):
        controller = RetryController(lambda *_: None, threading.Event())
        with patch.object(controller, 'wait') as wait:
            self.assertEqual(controller.delay('http', 0), 0)
            self.assertEqual(controller.delay('http', 1), 0)
            self.assertEqual([call.args[0] for call in wait.call_args_list], [2, 4])
            controller.observe(http_error(503, {'Retry-After': '12'}))
            controller.delay('http')
            self.assertEqual(wait.call_args.args[0], 12)

    def test_permission_certificate_and_long_wait_do_not_blindly_retry(self):
        controller = RetryController(lambda *_: None, threading.Event())
        errors = [http_error(status) for status in (401, 403, 404, 410)]
        errors += [CertificateVerifyError('fixture'), http_error(503, {'Retry-After': '120'})]
        with patch.object(controller, 'wait') as wait:
            for error in errors:
                controller.observe(error)
                with self.subTest(error=error), self.assertRaises(type(error)):
                    controller.delay('http')
            wait.assert_not_called()

    def test_wait_cancel_and_per_item_skip_are_prompt_and_report_countdown(self):
        for skip in (False, True):
            cancel = threading.Event()
            control = ItemControl(cancel)
            started = threading.Event()
            events, errors = [], []
            def notify(kind, data):
                events.append((kind, data))
                if kind == 'progress':
                    started.set()
            controller = RetryController(notify, control)
            def run():
                try:
                    controller.wait(10, '连接中断', 1, 'http')
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(started.wait(2))
            stamp = time.monotonic()
            (control.skip if skip else cancel).set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - stamp, .7)
            self.assertIsInstance(errors[0], Cancelled)
            self.assertEqual(events[-1][1]['retry_seconds'], 10)
            self.assertNotIn('speed', events[-1][1])

    def test_exhausted_429_is_not_retried_again_by_fragment_layer(self):
        controller = RetryController(lambda *_: None, threading.Event())
        error = http_error(429)
        error._shiying_retry_exhausted = True
        controller.observe(error)
        with patch.object(controller, 'wait') as wait, self.assertRaises(HTTPError):
            controller.delay('fragment')
        wait.assert_not_called()

    def test_post_request_is_never_automatically_replayed(self):
        opts = options(lambda *_: None, threading.Event())
        with RetryYoutubeDL(opts) as ydl, patch.object(yt_dlp.YoutubeDL, 'urlopen', side_effect=http_error(429)) as send:
            with self.assertRaises(HTTPError):
                ydl.urlopen(Request('https://example.test/action', data=b'fixture'))
            self.assertEqual(send.call_count, 1)

    def test_file_access_retry_ignores_unrelated_network_failure(self):
        controller = RetryController(lambda *_: None, threading.Event())
        controller.observe(http_error(404))
        with patch.object(controller, 'wait') as wait:
            self.assertEqual(controller.delay('file_access'), 0)
            self.assertEqual(wait.call_args.args[0], 1)


class AdapterContractTests(unittest.TestCase):
    def test_registry_rejects_similar_hosts_credentials_and_nonstandard_ports(self):
        self.assertEqual(len({adapter.key for adapter in ADAPTERS}), len(ADAPTERS))
        for url in ('https://vimeo.com.evil.test/123', 'https://pornhub.com.evil.test/video',
                    'https://notpornhub.com/video', 'https://www.douyin.com:8443/video/123',
                    'https://user:pass@vimeo.com/123', 'file://vimeo.com/123'):
            self.assertIsNone(adapter_for(url), url)
        self.assertEqual(adapter_for('https://cn.pornhub.com/video').key, 'pornhub')

    def test_fallback_plan_owns_extractor_and_keeps_original_resource_id(self):
        plan = fallback_for('https://www.douyin.com/video/6982497745948921092', [RuntimeError('Fresh cookies needed')])
        self.assertEqual(plan.expected_id, '6982497745948921092')
        with patch.object(yt_dlp.YoutubeDL, 'add_info_extractor') as add, yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            before = add.call_count
            plan.configure(ydl)
            self.assertEqual(add.call_count, before + 1)
            self.assertEqual(add.call_args.args[0].ie_key(), 'Douyin')

    def test_download_adapter_preserves_referer_and_is_scoped(self):
        info = {'http_headers': {'user-agent': 'old', 'Referer': 'keep'},
                'formats': [{'http_headers': None}, {'http_headers': {'Origin': 'keep', 'User-Agent': 'old'}}]}
        opts = {'http_headers': {'User-Agent': 'old', 'Referer': 'keep'}}
        original = copy.deepcopy(info)
        apply_download_compat('https://example.test/video', opts, info, lambda *_: None)
        self.assertEqual(info, original)
        apply_download_compat('https://cn.pornhub.com/video', opts, info, lambda *_: None)
        self.assertEqual(info['http_headers'], {'Referer': 'keep'})
        self.assertEqual(info['formats'][1]['http_headers'], {'Origin': 'keep'})
        self.assertEqual(opts['http_headers'], {'Referer': 'keep'})
        self.assertEqual(opts['impersonate'].client, 'safari')


class RetryTrafficTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_fixtures()
        cls.counts = Counter()
        cls.fragment_failure = None
        class RetryHandler(Handler):
            def do_GET(self):
                mode = self.path.split('?')[0].split('/')[-1]
                if cls.fragment_failure and mode.endswith('.ts'):
                    identity = f'{cls.fragment_failure}:{mode}'
                    cls.counts[identity] += 1
                    status = cls.fragment_failure
                    if status == 429 or cls.counts[identity] == 1:
                        self.send_response(status)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                if mode.startswith(('once503', 'once429', 'always429', 'always503', 'long429', 'denied')):
                    cls.counts[mode] += 1
                    status = (503 if '503' in mode else 403 if mode.startswith('denied') else 429)
                    if not mode.startswith('once') or cls.counts[mode] == 1:
                        self.send_response(status)
                        self.send_header('Retry-After', '120' if mode.startswith('long') else '1')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    self.path = '/first.mp4'
                return super().do_GET()
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(RetryHandler, directory=str(FIXTURES)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def item(self, name):
        return Video.from_info({'id': name, 'title': name, 'extractor_key': 'Fixture', 'duration': 3,
                                'formats': [dict(fmt(name, height=360, audio='aac'), url=self.base + '/' + name)]}, self.base + '/page')

    def test_actual_503_and_429_retry_to_decodable_mp4(self):
        for status in (503, 429):
            name = f'once{status}.mp4'
            events = []
            with tempfile.TemporaryDirectory() as folder, patch.object(RetryController, 'wait') as wait:
                path = download(self.item(name), 360, Path(folder), threading.Event(), lambda *event: events.append(event))
                self.assertEqual(path.suffix, '.mp4')
                self.assertIn('h264', inspect(path))
                self.assertEqual(self.counts[name], 2)
                self.assertEqual(wait.call_count, 1)
                self.assertEqual(wait.call_args.args[0], 5 if status == 429 else 2)

    def test_persistent_errors_are_bounded_and_never_publish_mp4(self):
        for name, expected in (('always429.mp4', 3), ('always503.mp4', 3), ('long429.mp4', 1), ('denied.mp4', 1)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder, patch.object(RetryController, 'wait'):
                with self.assertRaises(Exception):
                    download(self.item(name), 360, Path(folder), threading.Event(), lambda *_: None)
                self.assertEqual(self.counts[name], expected)
                self.assertFalse(list(Path(folder).glob('*.mp4')))

    def test_webm_source_still_produces_h264_aac_mp4(self):
        item = Video.from_info({'id': 'webm', 'title': 'webm', 'duration': 3, 'extractor_key': 'Fixture',
                                'formats': [dict(fmt('webm', height=180, codec='vp9', audio='opus'),
                                                 ext='webm', url=self.base + '/second.webm')]}, self.base + '/page')
        with tempfile.TemporaryDirectory() as folder:
            path = download(item, 180, Path(folder), threading.Event(), lambda *_: None, Network(keep_progress=True))
            details = inspect(path)
            self.assertEqual(path.suffix, '.mp4')
            self.assertIn('h264', details)
            self.assertIn('aac', details)

    def test_hls_retries_missing_segments_without_publishing_incomplete_output(self):
        item = analyze(self.base + '/master.m3u8', threading.Event(), lambda *_: None)[0]
        try:
            type(self).fragment_failure = 503
            with tempfile.TemporaryDirectory() as folder, patch.object(RetryController, 'wait') as wait:
                path = download(item, 180, Path(folder), threading.Event(), lambda *_: None)
                self.assertIn('h264', inspect(path))
                self.assertGreaterEqual(wait.call_count, 1)
            type(self).fragment_failure = 429
            with tempfile.TemporaryDirectory() as folder, patch.object(RetryController, 'wait'):
                with self.assertRaises(Exception):
                    download(item, 180, Path(folder), threading.Event(), lambda *_: None)
                self.assertFalse(list(Path(folder).glob('*.mp4')))
                attempted = [count for key, count in self.counts.items() if key.startswith('429:')]
                self.assertEqual(attempted, [3])
        finally:
            type(self).fragment_failure = None


class RetryGuideTests(WindowTestCase):
    def test_countdown_ui_and_in_app_instructions_are_readable(self):
        self.window.handle_event('progress', {'phase': '等待重试', 'percent': None,
            'retry_seconds': 5, 'retry_reason': '网站要求放慢请求（HTTP 429）', 'retry_attempt': 1})
        label = self.window.progress_label.text()
        self.assertIn('5 秒后重试', label)
        self.assertIn('429', label)
        self.assertIn('可以取消', label)
        self.assertNotIn('B/s', label)
        from video_demo.usage_guide import guide_topics
        topics = {topic.key: topic for topic in guide_topics()}
        self.assertIn('最终只保存为', topics['quality'].content)
        self.assertIn('倒计时', topics['failure'].content)


if __name__ == '__main__':
    unittest.main()
