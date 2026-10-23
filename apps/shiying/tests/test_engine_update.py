"""Isolated wheel staging, state recovery and manual UI contracts."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import contextlib
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from tests.test_workflow_additions import WindowTestCase
from video_demo import engine_state as state
from video_demo.engine_panel import EnginePanel
from video_demo.engine_update import Candidate, _extract, _remove_owned, check_update, install_candidate
from video_demo.network_retry import Cancelled
from video_demo.system_dialog import SystemDialog


def wheel_bytes(version='2026.12.1', extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('yt_dlp/__init__.py', '# isolated fixture\n')
        archive.writestr(f'yt_dlp-{version}.dist-info/METADATA', f'Name: yt-dlp\nVersion: {version}\n')
        if extra:
            archive.writestr(*extra)
    return stream.getvalue()


class Response:
    status_code = 200
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def raise_for_status(self):
        pass
    def iter_content(self, _):
        yield self.data


class Session(Response):
    def get(self, *args, **kwargs):
        return Response(self.data)


def candidate(data, version='2026.12.1'):
    return Candidate(version, f'yt_dlp-{version}-py3-none-any.whl',
                     'https://files.pythonhosted.org/packages/fixture.whl', hashlib.sha256(data).hexdigest(), len(data), 'stable')


class EngineStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'engine'
        self.original_path = list(__import__('sys').path)

    def tearDown(self):
        import sys
        sys.path[:] = self.original_path
        for key, handle in list(state._LEASES.items()):
            if key[0] == str(self.root):
                handle.close()
                del state._LEASES[key]
        self.temp.cleanup()

    def slot(self):
        slot = 'v-2026.12.1-123456789abc'
        folder = self.root / slot
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'engine.json').write_text(json.dumps(dict(owner=state.OWNER, slot=slot, validated=True)))
        return slot

    def pending(self):
        slot = self.slot()
        record = state.read_state(self.root)
        record['pending'] = slot
        state.write_state(record, self.root)
        return slot

    def test_no_state_startup_does_not_create_records(self):
        self.assertEqual(state.prepare_startup(self.root), '')
        self.assertFalse(self.root.exists())

    def test_pending_version_is_validated_then_confirmed_and_previous_retained(self):
        slot = self.pending()
        with patch.object(state, 'probe', return_value={'contracts': True}) as probe:
            state.prepare_startup(self.root)
        self.assertEqual(probe.call_count, 1)
        record = state.read_state(self.root)
        self.assertEqual((record['active'], record['previous'], record['trial']), (slot, 'base', slot))
        self.assertTrue(state.in_use(slot, self.root))
        state.confirm_start(self.root)
        self.assertIsNone(state.read_state(self.root)['trial'])
        restored = state.schedule_restore(self.root)
        self.assertEqual(restored['pending'], 'base')

    def test_failed_validation_does_not_activate_new_package(self):
        self.pending()
        with patch.object(state, 'probe', side_effect=state.EngineError('fixture fail')):
            notice = state.prepare_startup(self.root)
        self.assertEqual(state.read_state(self.root)['active'], 'base')
        self.assertIn('恢复', notice)
        self.assertEqual(__import__('sys').path, self.original_path)

    def test_interrupted_first_launch_rolls_back_before_imports(self):
        slot = self.pending()
        record = state.read_state(self.root)
        record.update(active=slot, previous='base', trial=slot, pending=None)
        state.write_state(record, self.root)
        with patch.object(state, 'probe') as probe:
            state.prepare_startup(self.root)
        probe.assert_not_called()
        self.assertEqual(state.read_state(self.root)['active'], 'base')

    def test_cancel_pending_and_repair_corrupt_pointer_do_not_delete_packages(self):
        slot = self.pending()
        self.assertIsNone(state.schedule_restore(self.root)['pending'])
        (self.root / 'state.json').write_text('BROKEN')
        repaired = state.schedule_restore(self.root)
        self.assertEqual(repaired['pending'], 'base')
        self.assertTrue((self.root / slot).is_dir())

    def test_unsafe_selection_and_unknown_cleanup_target_are_refused(self):
        self.root.mkdir()
        (self.root / 'state.json').write_text(json.dumps({'schema': 1, 'active': '../outside'}))
        with self.assertRaises(state.EngineError):
            state.read_state(self.root)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        with self.assertRaises(state.EngineError):
            _remove_owned(outside, self.root)
        self.assertTrue(outside.exists())

    def test_probe_rejects_success_from_the_wrong_python_package_location(self):
        slot = self.slot()
        output = json.dumps({'version': '2026.12.1', 'module_path': str(Path(self.temp.name) / 'outside.py')}).encode()
        with patch.object(state.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output)):
            with self.assertRaises(state.EngineError):
                state.probe(self.root / slot)

    def test_probe_timeout_is_reported_without_modifying_state(self):
        with patch.object(state.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 1)):
            with self.assertRaisesRegex(state.EngineError, '时间限制'):
                state.probe(None)
        self.assertFalse(self.root.exists())

    def test_initialization_failure_rejects_current_managed_version(self):
        slot = self.pending()
        record = state.read_state(self.root)
        record.update(active=slot, previous='base', pending=None, trial=None)
        state.write_state(record, self.root)
        self.assertTrue(state.reject_start(self.root))
        self.assertEqual(state.read_state(self.root)['active'], 'base')

    def test_another_process_runtime_lease_protects_loaded_files(self):
        slot = self.slot()
        marker = self.root / 'ready'
        code = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                'from video_demo.engine_state import _lease; h=_lease(sys.argv[3],Path(sys.argv[2])); '
                'Path(sys.argv[4]).write_text("ready"); sys.stdin.read(1); h.close()')
        process = subprocess.Popen([sys.executable, '-I', '-c', code, str(state.PROJECT), str(self.root), slot, str(marker)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            self.assertTrue(state.in_use(slot, self.root))
            process.communicate(input=b'x', timeout=5)
            self.assertFalse(state.in_use(slot, self.root))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


class EngineStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'engine'
        self.data = wheel_bytes()
        self.candidate = candidate(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, data=None, **kwargs):
        data = self.data if data is None else data
        with patch('video_demo.engine_update._session', return_value=Session(data)), \
             patch('video_demo.engine_update.probe', return_value={'version': '2026.12.1', 'mp4': True}) as probe:
            result = install_candidate(candidate(data), threading.Event(), root=self.root, **kwargs)
        return result

    def test_install_is_staged_and_does_not_change_running_engine_or_requirements(self):
        from video_demo.engine_update import current_version
        before = current_version()
        requirements = Path('requirements.txt').read_bytes()
        self.install()
        record = state.read_state(self.root)
        self.assertEqual(record['active'], 'base')
        self.assertIsNotNone(record['pending'])
        self.assertTrue(state.slot_path(record['pending'], self.root).is_dir())
        self.assertEqual(current_version(), before)
        self.assertEqual(Path('requirements.txt').read_bytes(), requirements)
        self.assertFalse(list(self.root.glob('.incoming-*')))

    def test_hash_mismatch_leaves_original_state(self):
        with patch('video_demo.engine_update._session', return_value=Session(b'corrupt')):
            with self.assertRaises(state.EngineError):
                install_candidate(self.candidate, threading.Event(), root=self.root)
        self.assertEqual(state.read_state(self.root)['active'], 'base')
        self.assertIsNone(state.read_state(self.root)['pending'])

    def test_failed_probe_and_cancel_after_probe_never_schedule_activation(self):
        for failure in (True, False):
            cancel = threading.Event()
            def probe(*args, **kwargs):
                if failure:
                    raise state.EngineError('fixture validation failure')
                cancel.set()
                return {'version': '2026.12.1', 'mp4': True}
            with patch('video_demo.engine_update._session', return_value=Session(self.data)), \
                 patch('video_demo.engine_update.probe', side_effect=probe):
                with self.assertRaises((state.EngineError, Cancelled)):
                    install_candidate(self.candidate, cancel, root=self.root)
            self.assertIsNone(state.read_state(self.root)['pending'])
            self.assertFalse(list(self.root.glob('v-*')))

    def test_path_traversal_foreign_package_and_version_mismatch_are_rejected(self):
        for extra in (('../outside.py', 'bad'), ('bad.pth', 'bad')):
            with self.subTest(extra=extra), self.assertRaises(state.EngineError):
                self.install(wheel_bytes(extra=extra))
        package = Path(self.temp.name) / 'package'
        package.mkdir()
        wheel = Path(self.temp.name) / 'fixture.whl'
        wheel.write_bytes(self.data)
        with self.assertRaises(state.EngineError):
            _extract(wheel, package, '2026.12.2')
        self.assertFalse((Path(self.temp.name) / 'outside.py').exists())

    def test_official_wheel_auxiliary_data_is_ignored_without_touching_path(self):
        data = wheel_bytes(extra=('yt_dlp-2026.12.1.data/data/share/man/man1/yt-dlp.1', 'manual'))
        self.install(data)
        root = state.slot_path(state.read_state(self.root)['pending'], self.root)
        self.assertTrue((root / 'yt_dlp/__init__.py').is_file())
        self.assertFalse(any(item.name.endswith('.data') for item in root.iterdir()))

    def test_pending_install_is_not_replaced_and_can_be_cancelled(self):
        self.install()
        before = state.read_state(self.root)['pending']
        with self.assertRaises(state.EngineError):
            self.install()
        self.assertEqual(state.read_state(self.root)['pending'], before)
        self.assertIsNone(state.schedule_restore(self.root)['pending'])

    def test_atomic_pointer_write_failure_removes_only_new_slot(self):
        with patch('video_demo.engine_update._session', return_value=Session(self.data)), \
             patch('video_demo.engine_update.probe', return_value={'version': '2026.12.1', 'mp4': True}), \
             patch('video_demo.engine_update.write_state', side_effect=OSError('fixture write denied')):
            with self.assertRaises(OSError):
                install_candidate(self.candidate, threading.Event(), root=self.root)
        self.assertFalse(list(self.root.glob('v-*')))

    def test_official_version_check_filters_yanked_foreign_and_older_stable(self):
        valid = {'filename': 'yt_dlp-2026.12.1-py3-none-any.whl', 'url': self.candidate.url,
                 'digests': {'sha256': self.candidate.sha256}, 'size': len(self.data), 'requires_python': '>=3.12'}
        metadata = {'releases': {'2026.12.1': [valid], '2027.1.1': [{**valid, 'yanked': True}],
                                 '2027.2.1': [{**valid, 'url': 'https://evil.test/package.whl'}]}}
        with patch('video_demo.engine_update._session', return_value=Session(json.dumps(metadata).encode())), \
             patch('video_demo.engine_update.current_version', return_value='2026.12.1.1.dev0'):
            result = check_update()
        self.assertEqual(result['candidate'].version, '2026.12.1')
        self.assertFalse(result['available'])


class EnginePanelTests(WindowTestCase):
    def test_open_is_offline_and_explicit_check_does_not_install(self):
        with patch('video_demo.engine_panel.check_update') as check, patch('video_demo.engine_panel.install_candidate') as install:
            dialog = SystemDialog(self.window, tab=2)
            panel = dialog.engine_panel
            check.assert_not_called()
            install.assert_not_called()
            self.assertFalse(panel.install_button.isEnabled())
            check.return_value = {'current': '1', 'candidate': candidate(wheel_bytes()), 'available': True}
            panel.check_button.click()
            self.wait(lambda: panel.job is None)
            self.assertTrue(panel.install_button.isEnabled())
            install.assert_not_called()
            dialog.reject()
            dialog.deleteLater()

    def test_closing_during_check_cancels_and_waits_for_job(self):
        def check(channel, proxy, cancel):
            while not cancel.wait(.01):
                pass
            raise Cancelled()
        with patch('video_demo.engine_panel.check_update', side_effect=check):
            dialog = SystemDialog(self.window, tab=2)
            dialog.engine_panel.check_button.click()
            dialog.reject()
            self.assertTrue(dialog.closing)
            self.wait(lambda: dialog.engine_panel.job is None)
            self.assertIn('取消', dialog.engine_panel.status.text())
            dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
