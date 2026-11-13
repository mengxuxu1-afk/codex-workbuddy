"""Synthetic transport tests, real SQLite/files; never claim host wake verification."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import desktop_bridge as bridge
import workbuddy_local as local
from project_sync import Collaboration, ROOT

C = '11111111-1111-4111-8111-111111111111'
W = '22222222-2222-4222-8222-222222222222'


class DesktopDispatch(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.work/collaboration_20261110/dispatch_tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'AGENTS.md').write_text('fixture', encoding='utf-8')
        self.service = Collaboration(self.root)
        self.service.init()
        self.config = dict(enabled=True, root=str(self.root), sessions=dict(codex=C, workbuddy=W), workbuddy_route_verified=True)
        self.write_config()
        self.prepared = patch.object(bridge, 'prepare', return_value='fixture-capability')
        self.prepared.start()
        self.addCleanup(self.prepared.stop)
        self.submitted = patch.object(bridge, 'submit', return_value='fixture-receipt')
        self.submit = self.submitted.start()
        self.addCleanup(self.submitted.stop)
        self.task = self.service.message('codex', C, 'workbuddy', dict(key='task.smoke', summary='fixture read-only task'))['message_id']

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self):
        (self.root / '.collab/desktop.json').write_text(json.dumps(self.config), encoding='utf-8')

    def test_explicit_connection_check_never_submits_or_claims_delivery(self):
        before=self.service.status()
        result=self.service.connection_check('codex',C,'workbuddy')
        self.assertEqual(result['status'],'live_at_check')
        self.assertFalse(result['notification_sent'])
        self.assertFalse(result['idle_wake_verified'])
        self.submit.assert_not_called()
        after=self.service.status()
        self.assertEqual(after['desktop_deliveries'],before['desktop_deliveries'])
        self.assertEqual(after['events'],before['events'])
        self.assertEqual(after['lease'],before['lease'])

    def test_connection_check_missing_registration_is_blocked_not_online(self):
        with patch.object(bridge,'prepare',side_effect=bridge.BridgeBlocked('safe reason',code='session_not_registered')):
            result=self.service.connection_check('codex',C,'workbuddy')
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['code'],'session_not_registered')
        self.submit.assert_not_called()
        self.assertEqual(self.service.status()['desktop_deliveries'],[])

    def test_connection_check_requires_sender_project_binding_before_preflight(self):
        with patch.object(bridge,'prepare') as prepare:
            with self.assertRaises(bridge.BridgeBlocked): self.service.connection_check('codex',W,'workbuddy')
            prepare.assert_not_called()

    def test_status_connection_check_stays_a_scoped_helper_command(self):
        command=f'"{sys.executable}" "{self.root / "setup/collaboration/project_sync.py"}" status --agent codex --session {C} --check-connection workbuddy'
        self.assertTrue(self.service.helper_command({'cmd':command},'codex',C))
        self.assertFalse(self.service.helper_command({'cmd':command+'; echo injected'},'codex',C))
        self.assertFalse(self.service.helper_command({'cmd':command.replace(' status ', ' dispatch ')},'codex',C))
        self.assertFalse(self.service.helper_command({'cmd':command.replace('--check-connection workbuddy','--check-connection foreign')},'codex',C))
        self.assertFalse(self.service.helper_command({'cmd':command.replace(C,W)},'codex',C))

    def test_real_status_cli_reports_blocked_and_nonzero_without_delivery(self):
        folder=self.root/'setup/collaboration';folder.mkdir(parents=True)
        for name in ['project_sync.py','desktop_bridge.py','workbuddy_local.py']:
            shutil.copy2(ROOT/'setup/collaboration'/name,folder/name)
        before=self.service.status()
        process=subprocess.run([sys.executable,'-X','utf8',str(folder/'project_sync.py'),
            'status','--agent','codex','--session',C,'--check-connection','workbuddy'],
            capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(process.returncode,2,process.stderr)
        result=json.loads(process.stdout)['connection_check']
        self.assertEqual(result['status'],'blocked')
        self.assertFalse(result['notification_sent'])
        after=self.service.status()
        self.assertEqual(after['events'],before['events'])
        self.assertEqual(after['desktop_deliveries'],before['desktop_deliveries'])

    def test_plan_result_and_local_rework_use_same_real_event_chain(self):
        first = self.service.dispatch('codex', C, self.task)
        self.assertEqual(first['status'], 'sent_awaiting_ack')
        self.assertFalse(first['wake_verified'])
        report = self.service.sync('workbuddy', W)
        self.service.ack('workbuddy', W, report['ack_token'])
        self.assertEqual(self.service.begin('workbuddy', W)['status'], 'editing')
        (self.root / 'AGENTS.md').write_text('fixture edited', encoding='utf-8')
        self.assertEqual(self.service.finish('workbuddy', W, dict(summary='actual fixture file changed'))['changes'], 1)
        result = self.service.message('workbuddy', W, 'codex', dict(key='result.smoke', summary='fixture result', reply_to=self.task))['message_id']
        self.assertEqual(self.service.dispatch('workbuddy', W, result)['status'], 'sent_awaiting_ack')
        review = self.service.sync('codex', C)
        changes = [e['changes'] for e in review['events'] if e['kind'] == 'handoff']
        self.assertEqual(changes[0][0]['path'], 'AGENTS.md')
        revision = self.service.message('codex', C, 'workbuddy', dict(key='task.smoke.r1', summary='fix one fixture field', reply_to=result))['message_id']
        self.assertEqual(self.service.dispatch('codex', C, revision)['status'], 'sent_awaiting_ack')
        self.assertEqual(self.submit.call_count, 3)

    def test_duplicate_submission_is_not_repeated(self):
        self.service.dispatch('codex', C, self.task)
        again = self.service.dispatch('codex', C, self.task)
        self.assertTrue(again['reused'])
        self.assertEqual(self.submit.call_count, 1)

    def received_notice(self, event, agent='workbuddy', session=W):
        report = self.service.sync(agent, session)
        self.service.ack(agent, session, report['ack_token'])
        return dict(notification=bridge.notification(self.root, session, event))

    def test_actual_recipient_can_resolve_unknown_without_resending(self):
        self.submit.side_effect = ValueError('fixture ambiguous queue receipt')
        self.service.dispatch('codex', C, self.task)
        notice = self.received_notice(self.task)
        result = self.service.receive('workbuddy', W, self.task, notice)
        self.assertEqual(result['status'], 'recipient_received')
        self.assertFalse(result['task_accepted'])
        self.assertTrue(self.service.receive('workbuddy', W, self.task, notice)['reused'])
        self.assertEqual(self.service.dispatch('codex', C, self.task)['status'], 'recipient_received')
        self.assertEqual(self.submit.call_count, 1)

    def test_receive_requires_matching_notice_recipient_ack_and_real_attempt(self):
        with self.assertRaises(ValueError):
            self.service.receive('workbuddy', W, self.task, dict(notification=bridge.notification(self.root,W,self.task)))
        notice = self.received_notice(self.task)
        with self.assertRaises(ValueError): self.service.receive('workbuddy', W, self.task, notice)
        self.service.dispatch('codex', C, self.task)
        with self.assertRaises(ValueError): self.service.receive('codex', C, self.task, notice)
        with self.assertRaises(ValueError): self.service.receive('workbuddy', W, self.task, dict(notification='different project'))
        self.assertEqual(self.service.status()['desktop_deliveries'][0]['status'], 'sent_awaiting_ack')

    def test_late_transport_completion_cannot_overwrite_recipient_confirmation(self):
        def send(*args, **kwargs):
            kwargs['accepted']()
            notice = self.received_notice(self.task)
            self.service.receive('workbuddy', W, self.task, notice)
            raise ValueError('fixture lost sender response after actual delivery')
        self.submit.side_effect = send
        self.assertEqual(self.service.dispatch('codex', C, self.task)['status'], 'recipient_received')
        self.assertEqual(self.submit.call_count, 1)

    def test_late_success_retains_receipt_without_downgrading_received(self):
        def send(*args, **kwargs):
            self.service.receive('workbuddy', W, self.task, self.received_notice(self.task))
            return 'late-fixture-transport-receipt'
        self.submit.side_effect = send
        output=self.service.dispatch('codex',C,self.task)
        self.assertEqual(output['status'],'recipient_received')
        self.assertEqual(output['receipt'],'late-fixture-transport-receipt')

    def test_unknown_submission_is_durable_and_never_retried(self):
        self.submit.side_effect = subprocess.TimeoutExpired('fixture', 15)
        self.assertEqual(self.service.dispatch('codex', C, self.task)['status'], 'unknown_no_resubmit')
        self.assertEqual(self.service.dispatch('codex', C, self.task)['status'], 'unknown_no_resubmit')
        self.assertEqual(self.submit.call_count, 1)

    def test_crash_after_claim_does_not_resubmit(self):
        self.submit.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.service.dispatch('codex', C, self.task)
        self.assertEqual(self.service.dispatch('codex', C, self.task)['status'], 'sending')
        self.assertEqual(self.submit.call_count, 1)

    def test_concurrent_dispatch_claims_only_one_submission(self):
        gate = threading.Barrier(2)
        def ready(*args): gate.wait(timeout=5); return 'fixture'
        with patch.object(bridge, 'prepare', side_effect=ready):
            with ThreadPoolExecutor(max_workers=2) as pool:
                output = list(pool.map(lambda _: self.service.dispatch('codex', C, self.task), range(2)))
        self.assertEqual(self.submit.call_count, 1)
        self.assertTrue(any(x['reused'] for x in output))

    def test_missing_authorization_does_not_claim_or_send(self):
        with patch.object(bridge, 'prepare', side_effect=bridge.BridgeBlocked('fixture missing authorization')):
            with self.assertRaises(bridge.BridgeBlocked): self.service.dispatch('codex', C, self.task)
        self.assertFalse(self.submit.called)
        self.assertEqual(self.service.dispatch('codex', C, self.task)['status'], 'sent_awaiting_ack')

    def test_workbuddy_result_uses_configured_executable_with_empty_path(self):
        self.prepared.stop()  # Exercise the actual resolver, mock only submission.
        executable = self.root / 'codex.exe'
        executable.write_bytes(b'fixture path, never executed')
        executable.chmod(0o700)
        self.config['codex_executable'] = str(executable)
        self.write_config()
        result = self.service.message('workbuddy', W, 'codex', dict(key='result.path', summary='fixture result', reply_to=self.task))['message_id']
        with patch.dict(bridge.os.environ, {'PATH': ''}):
            output = self.service.dispatch('workbuddy', W, result)
        self.assertEqual(output['status'], 'sent_awaiting_ack')
        self.assertEqual(self.submit.call_args.args[4], str(executable))
        self.assertEqual(self.service.status()['desktop_deliveries'][0]['event_id'], result)
        self.service.dispatch('workbuddy', W, result)
        self.assertEqual(self.submit.call_count, 1)

    def test_bad_codex_binding_blocks_result_before_delivery_claim(self):
        self.prepared.stop()
        self.config['codex_executable'] = str(self.root / 'missing-codex.exe')
        self.write_config()
        result = self.service.message('workbuddy', W, 'codex', dict(key='result.badpath', summary='fixture', reply_to=self.task))['message_id']
        with self.assertRaises(bridge.BridgeBlocked):
            self.service.dispatch('workbuddy', W, result)
        self.assertFalse(self.submit.called)
        self.assertEqual(self.service.status()['desktop_deliveries'], [])

    def test_unverified_route_allows_only_connection_probe(self):
        self.config['workbuddy_route_verified'] = False; self.write_config()
        with self.assertRaises(bridge.BridgeBlocked): self.service.dispatch('codex', C, self.task)
        self.assertFalse(self.submit.called)
        probe = self.service.message('codex', C, 'workbuddy', dict(key='task.bridge-probe.1', summary='read-only fixture route check'))['message_id']
        self.assertEqual(self.service.dispatch('codex', C, probe)['status'], 'sent_awaiting_ack')

    def test_source_change_after_plan_blocks_stale_assignment(self):
        (self.root / 'AGENTS.md').write_text('changed after plan', encoding='utf-8')
        with self.assertRaisesRegex(bridge.BridgeBlocked, '源码版本'): self.service.dispatch('codex', C, self.task)
        self.assertFalse(self.submit.called)

    def test_source_or_binding_change_during_preflight_does_not_submit(self):
        def drift(*args):
            (self.root / 'AGENTS.md').write_text('changed while waiting', encoding='utf-8')
            return 'fixture'
        with patch.object(bridge, 'prepare', side_effect=drift):
            with self.assertRaisesRegex(bridge.BridgeBlocked, '准备期间'): self.service.dispatch('codex', C, self.task)
        self.assertFalse(self.submit.called)

    def test_status_exposes_actual_transport_state_not_task_success(self):
        self.service.dispatch('codex', C, self.task)
        state = self.service.status()['desktop_deliveries']
        self.assertEqual(state[0]['status'], 'sent_awaiting_ack')
        self.assertEqual(state[0]['event_id'], self.task)

    def test_real_cli_legacy_transport_blocks_without_claiming_submission(self):
        folder = self.root / 'setup/collaboration'
        folder.mkdir(parents=True)
        for name in ['project_sync.py', 'desktop_bridge.py']:
            shutil.copy2(ROOT / 'setup/collaboration' / name, folder / name)
        self.config['workbuddy_transport'] = 'local_gateway'
        self.config['workbuddy_route_verified'] = False
        self.write_config()
        event = self.service.message('codex', C, 'workbuddy', dict(key='task.bridge-probe.cli', summary='read-only fixture'))['message_id']
        result = subprocess.run([sys.executable, '-X', 'utf8', str(folder/'project_sync.py'), 'dispatch',
                                 '--agent','codex','--session',C,'--event-id',str(event)],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('旧transport', result.stderr)
        with closing(sqlite3.connect(self.service.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM desktop_deliveries').fetchone()[0], 0)
        self.assertFalse(self.submit.called)

    def test_dispatch_requires_releasing_edit_lease(self):
        report = self.service.sync('codex', C)
        self.service.ack('codex', C, report['ack_token'])
        self.service.begin('codex', C)
        with self.assertRaises(bridge.BridgeBlocked): self.service.dispatch('codex', C, self.task)
        self.assertFalse(self.submit.called)

    def test_wrong_root_disabled_or_wrong_session_cannot_dispatch(self):
        for field, value in [('root', str(self.root.parent)), ('enabled', False)]:
            self.config[field] = value; self.write_config()
            with self.assertRaises(bridge.BridgeBlocked): self.service.dispatch('codex', C, self.task)
            self.config.update(root=str(self.root), enabled=True)
        self.write_config()
        with self.assertRaises(bridge.BridgeBlocked): self.service.dispatch('codex', W, self.task)
        self.assertFalse(self.submit.called)

    def test_foreign_event_and_general_notification_are_not_tasks(self):
        with self.assertRaises(ValueError): self.service.dispatch('workbuddy', W, self.task)
        note = self.service.message('codex', C, 'workbuddy', dict(key='status.note', summary='fixture note'))['message_id']
        with self.assertRaises(ValueError): self.service.dispatch('codex', C, note)
        result = self.service.message('workbuddy', W, 'codex', dict(key='result.unasked', summary='fixture'))['message_id']
        with self.assertRaises(ValueError): self.service.dispatch('workbuddy', W, result)
        self.assertFalse(self.submit.called)

    def test_no_more_than_two_rework_rounds(self):
        event = self.task
        for n in range(3):
            result = self.service.message('workbuddy', W, 'codex', dict(key=f'result.{n}', summary='fixture', reply_to=event))['message_id']
            event = self.service.message('codex', C, 'workbuddy', dict(key=f'task.{n}', summary='fixture local repair', reply_to=result))['message_id']
        with self.assertRaisesRegex(ValueError, '两次'): self.service.dispatch('codex', C, event)
        self.assertFalse(self.submit.called)

    def test_forked_rework_counts_against_one_root_budget(self):
        self.service.dispatch('codex', C, self.task)
        result = self.service.message('workbuddy', W, 'codex', dict(key='result.fork', summary='fixture', reply_to=self.task))['message_id']
        for n in range(2):
            event = self.service.message('codex', C, 'workbuddy', dict(key=f'task.fork.{n}', summary='fixture repair', reply_to=result))['message_id']
            self.service.dispatch('codex', C, event)
        third = self.service.message('codex', C, 'workbuddy', dict(key='task.fork.third', summary='fixture repair', reply_to=result))['message_id']
        with self.assertRaisesRegex(ValueError, '累计两次'): self.service.dispatch('codex', C, third)
        self.assertEqual(self.submit.call_count, 3)

    def test_editing_cannot_begin_between_claim_and_transport(self):
        report = self.service.sync('codex', C)
        self.service.ack('codex', C, report['ack_token'])
        def send(*args, **kwargs):
            self.assertEqual(self.service.begin('codex', C)['status'], 'blocked')
            return 'fixture-receipt'
        self.submit.side_effect = send
        self.service.dispatch('codex', C, self.task)
        self.assertEqual(self.service.begin('codex', C)['status'], 'editing')


class Transports(unittest.TestCase):
    def test_native_preflight_preserves_only_safe_fixed_diagnostic(self):
        config=dict(workbuddy_transport='native_reply')
        with patch.object(local,'preflight',side_effect=local.LocalBlocked('secret-sentinel',code='session_stale')):
            with self.assertRaises(bridge.BridgeBlocked) as caught:bridge.prepare(ROOT,config,'workbuddy')
        self.assertEqual(caught.exception.code,'session_stale')
        self.assertEqual(str(caught.exception),local.FAILURE_MESSAGES['session_stale'])
        self.assertNotIn('secret-sentinel',str(caught.exception))

    def test_legacy_transport_blocks_before_any_credential_or_network(self):
        # Retired enterprise OAuth / DPAPI / local_gateway must never be a
        # fallback: no credential file read, no HTTP worker, no registry read.
        for value in ['disabled', 'local_gateway', 'oauth', None, 42]:
            with self.subTest(transport=repr(value)):
                config = {} if value is None else {'workbuddy_transport': value}
                with patch.object(bridge, 'load_json') as credentials, \
                     patch.object(local, 'preflight') as preflight, \
                     patch.object(bridge.subprocess, 'run') as run:
                    with self.assertRaisesRegex(bridge.BridgeBlocked, '旧transport'):
                        bridge.prepare(ROOT, config, 'workbuddy')
                    credentials.assert_not_called()
                    preflight.assert_not_called()
                    run.assert_not_called()

    def test_explicit_codex_path_works_without_host_path(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'codex.exe'
            executable.write_bytes(b'fixture executable path, never executed')
            executable.chmod(0o700)
            with patch.object(bridge.shutil, 'which', return_value=None) as lookup:
                self.assertEqual(bridge.prepare(ROOT, {'codex_executable': str(executable)}, 'codex'), str(executable))
                lookup.assert_not_called()

    def test_invalid_explicit_codex_path_never_falls_back(self):
        for value in [None, '', 42, 'codex.exe', 'bad\x00path', str(ROOT / 'missing-codex.exe'), str(ROOT)]:
            with self.subTest(value=repr(value)), patch.object(bridge.shutil, 'which', return_value='other-codex.exe') as lookup:
                with self.assertRaisesRegex(bridge.BridgeBlocked, 'codex_executable'):
                    bridge.prepare(ROOT, {'codex_executable': value}, 'codex')
                lookup.assert_not_called()

    def test_codex_shell_wrappers_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            for suffix in ['.cmd', '.bat', '.ps1']:
                executable = Path(folder) / ('codex' + suffix)
                executable.write_text('fixture', encoding='utf-8')
                executable.chmod(0o700)
                with self.assertRaises(bridge.BridgeBlocked):
                    bridge.prepare(ROOT, {'codex_executable': str(executable)}, 'codex')

    def test_native_reply_is_the_only_accepted_workbuddy_transport(self):
        endpoint = dict(endpoint='http://127.0.0.1:1', session=W, root=str(ROOT),
                        pid=1234, transport='native_reply', executable='WorkBuddy.exe')
        with patch.object(local, 'preflight', return_value=endpoint) as preflight:
            self.assertEqual(bridge.prepare(ROOT, {'workbuddy_transport': 'native_reply'}, 'workbuddy'), endpoint)
            self.assertEqual(preflight.call_args.args[1]['workbuddy_transport'], 'native_reply')

    def test_valid_native_reply_dispatch_and_reply_simulation(self):
        # End-to-end simulation of prepare -> submit over the surviving route.
        endpoint = dict(endpoint='http://127.0.0.1:1', session=W, root=str(ROOT),
                        pid=1234, transport='native_reply', executable='WorkBuddy.exe')
        with patch.object(local, 'preflight', return_value=endpoint), \
             patch.object(local, 'submit', return_value='native-reply:fixture') as submit:
            prepared = bridge.prepare(ROOT, {'workbuddy_transport': 'native_reply',
                                             'workbuddy_local_registry_authorized': True,
                                             'workbuddy_process_gateway_authorized': True}, 'workbuddy')
            receipt = bridge.submit(ROOT, 'workbuddy', W, 42, prepared, connection_probe=True)
        self.assertEqual(receipt, 'native-reply:fixture')
        prompt = submit.call_args.args[1]
        self.assertIn('只读接通核验', prompt)
        self.assertNotIn('Bearer', prompt)
        self.assertNotIn('oauth', prompt.lower())

    def test_unknown_submit_shape_is_blocked_without_old_interface_fallback(self):
        with patch.object(bridge.subprocess, 'run') as run:
            with self.assertRaises(bridge.BridgeBlocked):
                bridge.submit(ROOT, 'unknown', W, 42, 'fixture')
            run.assert_not_called()

    def test_exact_queue_receipt_and_project_bound_command(self):
        receipt = '33333333-3333-4333-8333-333333333333'
        completed = subprocess.CompletedProcess([], 0, f'Queued message {receipt} for thread {C}.\n', '')
        with patch.object(bridge.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(bridge.submit(ROOT, 'codex', C, 42, 'codex.exe'), receipt)
            command = run.call_args.args[0]
            self.assertEqual(command[-2:], ['-C', str(ROOT)])
            self.assertEqual(command[command.index('--thread')+1], C)
            self.assertNotIn('--resume', command)
            self.assertEqual(run.call_args.kwargs['timeout'], 15)

    def test_success_exit_with_warning_or_wrong_session_is_unknown(self):
        for out, err in [('queued', ''), (f'Queued message {W} for thread {W}.', ''), (f'Queued message {W} for thread {C}.', 'warning')]:
            with patch.object(bridge.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, out, err)):
                with self.assertRaises(ValueError): bridge.submit(ROOT, 'codex', C, 42, 'codex.exe')

    def test_legacy_path_discovery_still_works(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'codex.exe'
            executable.write_bytes(b'fixture')
            executable.chmod(0o700)
            with patch.object(bridge.shutil, 'which', return_value=str(executable)):
                self.assertEqual(bridge.prepare(ROOT, {}, 'codex'), str(executable))
        with patch.object(bridge.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(bridge.BridgeBlocked, 'codex_executable'):
                bridge.prepare(ROOT, {}, 'codex')


if __name__ == '__main__':
    unittest.main(verbosity=2)
