"""Real SQLite/file/CLI tests in project-local synthetic workspaces, no host AI."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_sync import Collaboration, ROOT, snapshot, source_paths


class Cooperation(unittest.TestCase):
    def setUp(self):
        workspace = ROOT / '.work/collaboration_20261109/tests'
        workspace.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=workspace)
        self.root = Path(self.temporary.name).resolve()
        assert self.root.is_relative_to(ROOT)
        (self.root / 'AGENTS.md').write_text('fixture project rules', 'utf-8')
        (self.root / 'README.md').write_text('before', 'utf-8')
        self.service = Collaboration(self.root)
        self.service.init()

    def tearDown(self):
        self.temporary.cleanup()

    def read_all(self, agent, session):
        all_events = []
        while True:
            report = self.service.sync(agent, session)
            all_events.extend(report['events'])
            self.service.ack(agent, session, report['ack_token'])
            if not report['more']:
                return all_events

    def payload(self, event, session='w1', command=None, cwd=None):
        return dict(cwd=str(cwd or self.root), session_id=session, hook_event_name=event,
                    tool_name='Bash', tool_input={'command': command or 'echo test'})

    def test_two_way_real_files_and_hashes(self):
        self.read_all('workbuddy', 'w1')
        self.assertEqual(self.service.begin('workbuddy', 'w1')['status'], 'editing')
        (self.root / 'README.md').write_text('workbuddy change', 'utf-8')
        result = self.service.finish('workbuddy', 'w1', {'summary': 'changed README', 'tests': 'fixture test', 'remaining': ''})
        self.assertEqual(result['changes'], 1)
        events = self.read_all('codex', 'c1')
        change = [e for e in events if e['kind'] == 'handoff'][0]['changes'][0]
        self.assertEqual(change['path'], 'README.md')
        self.assertNotEqual(change['before']['sha256'], change['after']['sha256'])
        self.assertEqual(self.service.begin('codex', 'c1')['status'], 'editing')
        (self.root / 'README.md').write_text('codex change', 'utf-8')
        self.service.finish('codex', 'c1', {'summary': 'codex response'})
        self.assertTrue(any(e['actor'] == 'codex' and e['kind'] == 'handoff' for e in self.read_all('workbuddy', 'w1')))

    def test_delivery_is_not_ack_and_other_session_cannot_ack(self):
        report = self.service.sync('codex', 'c1')
        self.assertEqual(self.service.begin('codex', 'c1')['status'], 'blocked')
        with self.assertRaises(ValueError):
            self.service.ack('workbuddy', 'w1', report['ack_token'])
        with self.assertRaises(ValueError):
            self.service.ack('codex', 'c2', report['ack_token'])
        self.service.ack('codex', 'c1', report['ack_token'])
        self.assertEqual(self.service.begin('codex', 'c1')['status'], 'editing')

    def test_disk_change_after_ack_requires_resync(self):
        self.read_all('codex', 'c1')
        path = self.root / 'README.md'; before = path.stat()
        path.write_text('AFTER!', 'utf-8')  # Same byte count and restored timestamp.
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.service.begin('codex', 'c1')['status'], 'blocked')
        events = self.read_all('codex', 'c1')
        self.assertEqual(events[-1]['actor'], 'unattributed')
        self.assertEqual(self.service.begin('codex', 'c1')['status'], 'editing')

    def test_pagination_cannot_ack_unseen_page(self):
        for n in range(20):
            self.service.finish('workbuddy', 'w1', {'summary': str(n)})
        first = self.service.sync('codex', 'c1')
        self.assertEqual(len(first['events']), 8); self.assertTrue(first['more'])
        self.service.ack('codex', 'c1', first['ack_token'])
        self.assertEqual(self.service.begin('codex', 'c1')['status'], 'blocked')
        self.assertEqual(len(self.read_all('codex', 'c1')), 13)

    def test_concurrent_editors_only_one_wins(self):
        self.read_all('codex', 'c1'); self.read_all('workbuddy', 'w1')
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda args: Collaboration(self.root).begin(*args), [('codex', 'c1'), ('workbuddy', 'w1')]))
        self.assertEqual(sorted(r['status'] for r in results), ['blocked', 'editing'])

    def test_renew_does_not_reclassify_own_unfinished_edit(self):
        self.read_all('codex', 'c1'); self.service.begin('codex', 'c1')
        (self.root / 'README.md').write_text('in progress', 'utf-8')
        self.assertTrue(self.service.begin('codex', 'c1')['renewed'])
        report = self.service.sync('workbuddy', 'w1')
        observed = [e for e in report['events'] if e['kind'] == 'in_progress'][0]
        self.assertEqual(observed['actor'], 'unattributed')
        self.assertEqual(observed['details']['lease_owner'], 'codex:c1')
        result = self.service.finish('codex', 'c1', {'summary': 'done'})
        self.assertEqual(result['changes'], 1)  # Other side's sync must not consume the actual handoff diff.

    def test_stop_and_stale_do_not_steal_another_lease(self):
        self.read_all('codex', 'c1'); self.service.begin('codex', 'c1')
        with self.service.transaction() as db:
            db.execute('UPDATE lease SET heartbeat=?', (time.time() - 3600,))
        self.assertTrue(self.service.status()['lease']['stale'])
        self.assertEqual(self.service.hook('workbuddy', self.payload('Stop')), {})
        self.read_all('workbuddy', 'w1')
        self.assertEqual(self.service.begin('workbuddy', 'w1')['status'], 'blocked')
        with self.assertRaises(ValueError):
            self.service.finish('workbuddy', 'w1')
        self.service.hook('codex', self.payload('Stop', session='c1'))
        self.assertIsNone(self.service.status()['lease'])

    def test_helpers_only_exact_single_command_bypass(self):
        self.read_all('codex', 'c1'); self.service.begin('codex', 'c1')
        script = self.root / 'setup/collaboration/project_sync.py'
        command = f'"{sys.executable}" -X utf8 "{script}" sync --agent workbuddy --session w1'
        self.assertEqual(self.service.hook('workbuddy', self.payload('PreToolUse', command=command)), {})
        dispatch = command.replace(' sync ', ' dispatch ') + ' --event-id 42'
        self.assertEqual(self.service.hook('workbuddy', self.payload('PreToolUse', command=dispatch)), {})
        self.assertFalse(self.service.helper_command({'cmd': dispatch + '; echo injected'}, 'workbuddy', 'w1'))
        for bad in ['echo test # project_sync.py', command + '; echo injected', command.replace('--session w1', '--session c1'), 'echo project_sync.py']:
            result = self.service.hook('workbuddy', self.payload('PreToolUse', command=bad))
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_hook_context_bounded_and_no_auto_read_confirmation(self):
        hook = self.service.hook('workbuddy', self.payload('SessionStart'))
        context = json.loads(hook['hookSpecificOutput']['additionalContext'])
        self.assertLess(len(json.dumps(hook)), 3000)
        report_path = Path(context['report'])
        self.assertTrue(report_path.is_relative_to(self.root))
        self.assertTrue(json.loads(report_path.read_text('utf-8'))['events'])
        self.assertEqual(self.service.begin('workbuddy', 'w1')['status'], 'blocked')

    def test_scope_excludes_private_and_large_media(self):
        for relative in ['.work/private/key.txt', 'deployments/private.md', 'knowledge/private/secret.json', 'setup/.env', 'setup/config.local.json', 'knowledge/video.mp4']:
            path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('private', 'utf-8')
        self.assertEqual({p.relative_to(self.root).as_posix() for p in source_paths(self.root)}, {'AGENTS.md', 'README.md'})

    def test_dependency_locks_and_eval_datasets_are_tracked(self):
        for relative in ['.env.example', '.agents/skills/video-remix/requirements.lock',
                         '.agents/skills/video-remix/evals/datasets/example.jsonl']:
            path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture', 'utf-8')
        self.assertEqual(len(snapshot(self.root)), 5)

    def test_root_binding_outside_cwd_and_link_rejected(self):
        with self.assertRaises(ValueError):
            self.service.hook('workbuddy', self.payload('SessionStart', cwd=ROOT))
        with self.assertRaises(ValueError):
            self.service.hook('workbuddy', dict(session_id='w1', hook_event_name='SessionStart'))
        with self.service.transaction() as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', ('wrong-root', 'root'))
        with self.assertRaises(ValueError):
            self.service.status()

    def test_secret_note_rejected_without_releasing_lease(self):
        self.read_all('codex', 'c1'); self.service.begin('codex', 'c1')
        with self.assertRaises(ValueError):
            self.service.finish('codex', 'c1', {'summary': 'sk-' + 'a' * 32})
        self.assertEqual(self.service.status()['lease']['owner'], 'codex:c1')

    def test_active_peer_notification_without_scan_or_automatic_ack(self):
        self.read_all('codex', 'c1'); self.read_all('workbuddy', 'w1')
        sent = self.service.message('codex', 'c1', 'workbuddy', dict(key='hello-1', summary='Project message'))
        with patch('project_sync.snapshot', side_effect=AssertionError('notification must not scan files')):
            hook = self.service.hook('workbuddy', self.payload('PostToolUse'))
            context = json.loads(hook['hookSpecificOutput']['additionalContext'])
            self.assertEqual(context['notifications'][0]['id'], sent['message_id'])
            self.assertEqual(self.service.hook('workbuddy', self.payload('PostToolUse')), {})
        self.assertEqual(self.service.begin('workbuddy', 'w1')['status'], 'blocked')
        message = self.service.status()['messages'][0]
        self.assertEqual(message['acknowledged_by'], [])
        self.assertEqual(message['offered_to'], ['workbuddy:w1'])
        events = self.read_all('workbuddy', 'w1')
        self.assertEqual(events[0]['details']['summary'], 'Project message')
        self.assertEqual(self.service.status()['messages'][0]['acknowledged_by'], ['workbuddy:w1'])

    def test_messages_deduplicate_and_validate_reply(self):
        details = dict(key='same', summary='First message')
        first = self.service.message('codex', 'c1', 'workbuddy', details)
        self.assertTrue(self.service.message('codex', 'c1', 'workbuddy', details)['reused'])
        with self.assertRaises(ValueError):
            self.service.message('codex', 'c1', 'workbuddy', dict(key='same', summary='Changed'))
        with self.assertRaises(ValueError):
            self.service.message('codex', 'c1', 'codex', details)
        with self.assertRaises(ValueError):
            self.service.message('workbuddy', 'w1', 'codex', dict(key='bad', summary='x', reply_to=99999))
        reply = self.service.message('workbuddy', 'w1', 'codex', dict(key='reply', summary='Received', reply_to=first['message_id']))
        events = self.read_all('codex', 'c1')
        self.assertEqual(events[-1]['id'], reply['message_id'])
        self.assertEqual(events[-1]['details']['reply_to'], first['message_id'])

    def test_owner_observation_not_self_notification_and_finish_still_complete(self):
        self.read_all('codex', 'c1'); self.read_all('workbuddy', 'w1')
        self.service.begin('codex', 'c1')
        (self.root / 'README.md').write_text('edited by owner', 'utf-8')
        self.assertEqual(self.service.hook('codex', self.payload('PostToolUse', session='c1')), {})
        notice = self.service.hook('workbuddy', self.payload('PostToolUse'))
        context = json.loads(notice['hookSpecificOutput']['additionalContext'])
        self.assertEqual(context['notifications'][0]['kind'], 'in_progress')
        self.assertEqual(context['lease']['owner'], 'codex:c1')
        self.assertEqual(self.service.finish('codex', 'c1')['changes'], 1)

    def test_notification_pagination_and_explicit_ack_are_independent(self):
        self.read_all('workbuddy', 'w1')
        for n in range(19):
            self.service.message('codex', 'c1', 'workbuddy', dict(key=str(n), summary=str(n)))
        ids = []
        for unused in range(3):
            ids.extend(e['id'] for e in self.service.notify('workbuddy', 'w1')['notifications'])
        self.assertEqual(len(ids), 19); self.assertEqual(len(set(ids)), 19)
        self.assertEqual(self.service.notify('workbuddy', 'w1'), {})
        self.assertEqual(len(self.read_all('workbuddy', 'w1')), 19)

    def test_additive_upgrade_preserves_lease_events_and_ack(self):
        self.read_all('codex', 'c1'); self.service.begin('codex', 'c1')
        with self.service.transaction() as db:
            db.execute('DROP TABLE notifications')
        status = self.service.status()
        self.assertEqual(status['events'], 1)
        self.assertEqual(status['lease']['owner'], 'codex:c1')
        self.service.finish('codex', 'c1')
        self.assertEqual(self.service.begin('codex', 'c1')['status'], 'editing')

    def test_hook_age_not_permanent_online_flag_and_context_bounded(self):
        self.service.hook('codex', self.payload('PreToolUse', session='c1'))
        with self.service.transaction() as db:
            db.execute('UPDATE connections SET time=?', (time.time() - 300,))
        self.assertFalse(self.service.status()['connections'][0]['recent_hook'])
        for n in range(8):
            self.service.message('codex', 'c1', 'workbuddy', dict(key=str(n), summary='长' * 2000))
        result = self.service.notify('workbuddy', 'w1')
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 4000)

    def test_unacknowledged_offer_retried_after_lost_hook_output(self):
        self.read_all('workbuddy', 'w1')
        sent = self.service.message('codex', 'c1', 'workbuddy', dict(key='lost', summary='Retry me'))
        self.service.notify('workbuddy', 'w1')  # Emulate stdout lost after commit.
        self.assertEqual(self.service.notify('workbuddy', 'w1'), {})
        with self.service.transaction() as db:
            db.execute('UPDATE notification_rounds SET started=?', (time.time() - 31,))
        retry = self.service.notify('workbuddy', 'w1')
        self.assertEqual(retry['notifications'][0]['id'], sent['message_id'])
        self.read_all('workbuddy', 'w1')
        with self.service.transaction() as db:
            db.execute('UPDATE notification_rounds SET started=?', (time.time() - 31,))
        self.assertEqual(self.service.notify('workbuddy', 'w1'), {})

    def test_offer_receipt_does_not_infer_skipped_messages(self):
        first = self.service.message('codex', 'c1', 'workbuddy', dict(key='read-direct', summary='M1'))
        self.read_all('workbuddy', 'w1')
        self.service.message('codex', 'c1', 'workbuddy', dict(key='offered', summary='M2'))
        self.service.notify('workbuddy', 'w1')
        messages = {m['id']: m for m in self.service.status()['messages']}
        self.assertEqual(messages[first['message_id']]['offered_to'], [])

    def test_new_arrivals_do_not_postpone_old_unread_retry(self):
        self.read_all('workbuddy', 'w1')
        now = time.time()
        with patch('project_sync.time.time', return_value=now):
            first = self.service.message('codex', 'c1', 'workbuddy', dict(key='m1', summary='M1'))
            self.service.notify('workbuddy', 'w1')
        with patch('project_sync.time.time', return_value=now + 20):
            self.service.message('codex', 'c1', 'workbuddy', dict(key='m2', summary='M2'))
            self.service.notify('workbuddy', 'w1')
        with patch('project_sync.time.time', return_value=now + 40):
            self.service.message('codex', 'c1', 'workbuddy', dict(key='m3', summary='M3'))
            retry = self.service.notify('workbuddy', 'w1')
        self.assertIn(first['message_id'], [e['id'] for e in retry['notifications']])

    def test_debounced_dirty_source_flushed_by_read_tool(self):
        self.read_all('codex', 'c1'); self.service.begin('codex', 'c1')
        self.service.hook('codex', self.payload('PostToolUse', session='c1'))
        (self.root / 'README.md').write_text('second write inside debounce window', 'utf-8')
        with self.service.transaction() as db:
            db.execute("UPDATE meta SET value=? WHERE key='last_realtime_scan'", (str(time.time() + 1),))
        self.service.hook('codex', self.payload('PostToolUse', session='c1'))
        with self.service.transaction() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM meta WHERE key='realtime_dirty_owner'").fetchone())
            db.execute("UPDATE meta SET value=? WHERE key='last_realtime_scan'", (str(time.time() - 3),))
        payload = self.payload('PostToolUse', session='c1'); payload['tool_name'] = 'Read'
        self.service.hook('codex', payload)
        with self.service.transaction() as db:
            observed = db.execute("SELECT * FROM events WHERE kind='in_progress' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(json.loads(observed['changes'])[0]['path'], 'README.md')
            self.assertIsNone(db.execute("SELECT 1 FROM meta WHERE key='realtime_dirty_owner'").fetchone())


if __name__ == '__main__':
    unittest.main(verbosity=2)
