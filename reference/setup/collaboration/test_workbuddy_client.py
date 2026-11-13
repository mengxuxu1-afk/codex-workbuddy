"""Neutral client startup tests; no model, project pairing, or real GUI spawn."""
from contextlib import nullcontext
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workbuddy_client as client


class ClientTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[2] / '.work/client-startup-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.folder = Path(self.temp.name)
        self.exe = self.folder / 'WorkBuddy.exe'
        self.exe.write_bytes(b'fixture, not an executable')
        self.config = dict(executable=str(self.exe), sha256=hashlib.sha256(self.exe.read_bytes()).hexdigest(), port=9333)

    def tearDown(self):
        self.temp.cleanup()

    def test_reuse_same_pid_without_spawn_and_release_lock(self):
        lock = Mock(); lock.__enter__ = Mock(); lock.__exit__ = Mock(return_value=False)
        with patch.object(client, 'trusted_executable', return_value=True), \
             patch.object(client, '_NamedMutex', return_value=lock), \
             patch.object(client, 'renderer_pid', return_value=123), \
             patch.object(client, '_spawn') as spawn:
            first = client.ensure_client(self.config)
            second = client.ensure_client(self.config)
        self.assertEqual(first, dict(status='already_running', pid=123))
        self.assertEqual(first, second)
        spawn.assert_not_called()
        self.assertEqual(lock.__exit__.call_count, 2)

    def test_installation_alias_uses_the_same_mutex_and_renderer_path(self):
        (self.folder / 'nested').mkdir()
        alias = self.folder / 'nested' / '..' / 'WorkBuddy.exe'
        canonical, _, _ = client._validate(self.config)
        resolved_alias, _, _ = client._validate({**self.config, 'executable': str(alias)})
        self.assertEqual(canonical, resolved_alias)
        self.assertEqual(client._NamedMutex(canonical).name, client._NamedMutex(resolved_alias).name)

    def test_process_enumeration_failure_is_not_an_empty_process_list(self):
        if os.name != 'nt':
            self.skipTest('Windows process enumeration API')
        for first, error in [(False, 5), (True, 5), (False, 18)]:
            kernel = Mock(); kernel.CreateToolhelp32Snapshot.return_value = 123
            kernel.Process32FirstW.return_value = first; kernel.Process32NextW.return_value = False
            with patch.object(ctypes, 'WinDLL', return_value=kernel), \
                 patch.object(ctypes, 'get_last_error', return_value=error):
                if error == 18:
                    self.assertEqual(client.installed_processes(self.exe), [])
                else:
                    with self.assertRaisesRegex(client.ClientBlocked, 'enumerat'):
                        client.installed_processes(self.exe)
            kernel.CloseHandle.assert_called_once_with(123)

    def test_unconnected_running_client_is_never_restarted(self):
        with patch.object(client, 'trusted_executable', return_value=True), \
             patch.object(client, '_NamedMutex', return_value=nullcontext()), \
             patch.object(client, 'renderer_pid', return_value=None), \
             patch.object(client, 'installed_processes', return_value=[99]), \
             patch.object(client, '_spawn') as spawn:
            with self.assertRaisesRegex(client.ClientBlocked, 'exit it normally'):
                client.ensure_client(self.config)
        spawn.assert_not_called()

    def test_spawn_is_once_then_returns_observed_pid(self):
        with patch.object(client, 'trusted_executable', return_value=True), \
             patch.object(client, '_NamedMutex', return_value=nullcontext()), \
             patch.object(client, 'renderer_pid', side_effect=[None, 456]), \
             patch.object(client, 'installed_processes', return_value=[]), \
             patch.object(client, '_spawn') as spawn:
            result = client.ensure_client(self.config)
        self.assertEqual(result, dict(status='started', pid=456))
        spawn.assert_called_once_with(self.exe, 9333)

    def test_detached_process_and_no_console_or_global_environment_change(self):
        before = os.environ.get('WORKBUDDY_REMOTE_DEBUGGING_PORT')
        with patch.object(client.subprocess, 'Popen') as popen:
            client._spawn(self.exe, 9333)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], str(self.exe))
        self.assertEqual(kwargs['cwd'], self.exe.parent)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertTrue(kwargs['close_fds'])
        if os.name == 'nt':
            self.assertEqual(kwargs['creationflags'], client.DETACHED_PROCESS | client.CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(kwargs['env']['WORKBUDDY_REMOTE_DEBUGGING_PORT'], '9333')
        self.assertEqual(os.environ.get('WORKBUDDY_REMOTE_DEBUGGING_PORT'), before)

    def test_project_fields_invalid_port_signature_hash_rejected_before_spawn(self):
        with patch.object(client, '_spawn') as spawn, patch.object(client, 'trusted_executable', return_value=False):
            for extra in ['root', 'project_id', 'session_id', 'cwd']:
                with self.assertRaises(client.ClientBlocked):
                    client.ensure_client({**self.config, extra: 'forbidden'})
            for port in [True, 0, 65536, '9333']:
                with self.assertRaises(client.ClientBlocked):
                    client.ensure_client({**self.config, 'port': port})
            with self.assertRaisesRegex(client.ClientBlocked, 'changed'):
                client.ensure_client({**self.config, 'sha256': '0' * 64})
            with self.assertRaisesRegex(client.ClientBlocked, 'signature'):
                client.ensure_client(self.config)
        spawn.assert_not_called()

    def test_renderer_rejects_foreign_listener_and_pid_reuse(self):
        with patch.object(client, 'port_owner', return_value=123), \
             patch.object(client, 'process_image', return_value=self.folder / 'other.exe'):
            with self.assertRaisesRegex(client.ClientBlocked, 'different executable'):
                client.renderer_pid(self.exe, 9333)
        url = (self.exe.parent / 'resources/app.asar/renderer/index.html').as_uri() + '?account=DO_NOT_LOG'
        with patch.object(client, 'port_owner', return_value=123), \
             patch.object(client, 'process_image', return_value=self.exe.resolve()), \
             patch.object(client, 'process_birth', side_effect=[111, 222]), \
             patch.object(client, 'bounded_json', return_value=[dict(type='page', url=url)]):
            with self.assertRaisesRegex(client.ClientBlocked, 'changed during discovery'):
                client.renderer_pid(self.exe, 9333)

    def test_only_fixed_loopback_discovery_and_no_query_returned(self):
        url = (self.exe.parent / 'resources/app.asar/renderer/index.html').as_uri() + '?account=DO_NOT_LOG'
        with patch.object(client, 'port_owner', return_value=123), \
             patch.object(client, 'process_image', return_value=self.exe.resolve()), \
             patch.object(client, 'process_birth', return_value=111), \
             patch.object(client, 'bounded_json', return_value=[dict(type='page', url=url)]) as fetch:
            result = client.renderer_pid(self.exe, 9333)
        self.assertEqual(result, 123)
        fetch.assert_called_once_with('http://127.0.0.1:9333/json/list')

    def test_mutex_timeout_closes_without_release_and_abandoned_is_owned(self):
        if os.name != 'nt':
            self.skipTest('Windows mutex API')
        for wait, should_enter in [(client.WAIT_TIMEOUT, False), (client.WAIT_ABANDONED, True)]:
            kernel = Mock(); kernel.CreateMutexW.return_value = 123
            kernel.WaitForSingleObject.return_value = wait
            with patch.object(ctypes, 'WinDLL', return_value=kernel):
                if should_enter:
                    with self.assertRaisesRegex(RuntimeError, 'fixture'):
                        with client._NamedMutex(self.exe):
                            raise RuntimeError('fixture')
                    kernel.ReleaseMutex.assert_called_once_with(123)
                else:
                    with self.assertRaises(client.ClientBlocked):
                        with client._NamedMutex(self.exe):
                            self.fail('mutex timeout acquired ownership')
                    kernel.ReleaseMutex.assert_not_called()
            kernel.CloseHandle.assert_called_once_with(123)

    def test_cli_receipt_is_atomic_and_has_no_project_data(self):
        config = self.folder / 'client.json'
        config.write_text(json.dumps(self.config), 'utf8')
        with patch.object(client, 'ensure_client', return_value=dict(status='already_running', pid=123)):
            self.assertEqual(client.main(['--config', str(config), '--gui']), 0)
        self.assertEqual(json.loads((self.folder / 'last-launch.json').read_text('utf8')),
                         dict(status='already_running', pid=123))
        self.assertEqual(list(self.folder.glob('*.tmp')), [])

    def test_legacy_wrapper_projects_only_client_fields_and_never_selects(self):
        from workbuddy_desktop import launch_desktop
        config = dict(workbuddy_executable=str(self.exe), desktop_executable_sha256=self.config['sha256'],
                      desktop_cdp_port=9333, root='private-project', sessions={'workbuddy': 'private-session'})
        with patch('workbuddy_desktop.DesktopRuntime') as runtime, \
             patch('workbuddy_desktop.DesktopConnection') as connection, \
             patch.object(client, 'ensure_client', return_value=dict(status='already_running', pid=123)) as ensure:
            runtime.return_value.config.return_value = config
            self.assertEqual(launch_desktop(self.folder)['pid'], 123)
        ensure.assert_called_once_with(self.config)
        connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
