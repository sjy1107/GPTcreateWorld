"""Portable path stability and fixed-function worker contracts."""
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from video_demo import paths, preferences, engine_state, bootstrap
from video_demo.history import HistoryStore
from video_demo.queue_store import QueueStore
from video_demo.version import VERSION
from tests.test_stability import sample


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / '中文 软件'
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def portable(self, root=None):
        stack = ExitStack()
        stack.enter_context(patch.object(paths, 'frozen', return_value=True))
        stack.enter_context(patch.object(paths, 'app_root', return_value=root or self.root))
        return stack

    def test_current_version(self):
        self.assertEqual(VERSION, '0.1.0')

    def test_source_paths_are_unchanged(self):
        with patch.object(paths, 'frozen', return_value=False):
            self.assertEqual(paths.encode_path(self.root), str(self.root))
            self.assertEqual(paths.data_root().name, '.local')

    def test_portable_and_external_paths_are_distinct(self):
        with self.portable():
            self.assertEqual(paths.data_root(), self.root / 'data')
            self.assertEqual(paths.encode_path(self.root / 'downloads/中文.mp4'), 'portable:/downloads/中文.mp4')
            self.assertEqual(paths.decode_path('portable:/downloads/中文.mp4'), str(self.root / 'downloads/中文.mp4'))
            external = self.root.parent / 'external'
            self.assertEqual(paths.encode_path(external), str(external))

    def test_corrupt_relative_paths_are_rejected(self):
        with self.portable():
            for value in ('portable:/../escape', 'portable://absolute', 'portable:/C:/escape', 'portable:/a\\b'):
                with self.assertRaises(ValueError):
                    paths.decode_path(value)

    def test_runtime_files_use_bundle_resources(self):
        with patch.object(paths, 'frozen', return_value=True), patch('sys.executable', str(self.root / '拾影.exe')):
            self.assertEqual(paths.app_root(), self.root)
        (self.root / '_internal').mkdir()
        (self.root / 'portable.flag').write_text('')
        with patch.object(paths, 'frozen', return_value=True), patch('sys.executable', str(self.root / '_internal/ShiyingWorker.exe')):
            self.assertEqual(paths.app_root(), self.root)

    def test_portable_records_follow_folder_after_move(self):
        moved = self.root.parent / '移动后 中文'
        folder = self.root / 'downloads'
        folder.mkdir()
        media = folder / 'synthetic.mp4'
        media.write_bytes(b'local synthetic fingerprint fixture')
        data = self.root / 'data'
        with self.portable(), patch.object(preferences, 'PREFERENCES', data / 'preferences.json'):
            preferences.save_folder(folder)
            queue = QueueStore(data / 'queue.json')
            queue.enabled = True
            queue.save([], folder)
            HistoryStore(data / 'history.sqlite3').record(sample(), None, media)
            self.assertIn('portable:/downloads', (data / 'preferences.json').read_text())
        shutil.move(str(self.root), str(moved))
        with self.portable(moved), patch.object(preferences, 'PREFERENCES', moved / 'data/preferences.json'):
            self.assertEqual(preferences.load_folder(moved / 'downloads'), str(moved / 'downloads'))
            self.assertEqual(QueueStore(moved / 'data/queue.json').snapshot['folder'], str(moved / 'downloads'))
            self.assertEqual(HistoryStore(moved / 'data/history.sqlite3').recent()[0]['path'], str(moved / 'downloads/synthetic.mp4'))

    def test_frozen_probe_invokes_worker_not_python(self):
        internal = self.root / '_internal'
        internal.mkdir()
        (internal / 'ShiyingWorker.exe').write_bytes(b'fixture')
        result = type('Result', (), {'returncode': 0, 'stdout': b'{"version":"fixture"}'})()
        with patch.object(engine_state, 'frozen', return_value=True), patch.object(engine_state, 'PROJECT', self.root), \
                patch.object(engine_state.subprocess, 'run', return_value=result) as run:
            engine_state.probe(None)
            args = run.call_args.args[0]
            self.assertEqual(Path(args[0]), internal / 'ShiyingWorker.exe')
            self.assertEqual(args[1:], ['--engine-probe', '', 'quick'])
            self.assertNotIn('-c', args)

    def test_frozen_repair_is_plain_language(self):
        with patch.object(bootstrap, 'frozen', return_value=True):
            instructions = bootstrap.repair_steps()
            self.assertIn('完整解压', instructions)
            self.assertNotIn('pip install', instructions)


if __name__ == '__main__':
    unittest.main()
