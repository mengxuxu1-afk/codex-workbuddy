"""Exercise installed hook commands in cmd/Git Bash, using isolated fixture roots."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import statistics

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.work/collaboration_20261110'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='实际 Hook 空格_', dir=OUT) as name:
        root = Path(name).resolve(); assert root.is_relative_to(OUT)
        folder = root / 'setup/collaboration'; folder.mkdir(parents=True)
        script = folder / 'project_sync.py'
        shutil.copy2(ROOT / 'setup/collaboration/project_sync.py', script)
        (root / 'AGENTS.md').write_text('Synthetic project. Not a real WorkBuddy chat.', 'utf-8')
        (root / 'README.md').write_text('before', 'utf-8')
        checks = []; runs = []

        def check(name, value):
            checks.append(dict(name=name, ok=bool(value)))
            assert value, name

        def cli(operation, agent='codex', session='c1', *args):
            process = subprocess.run([sys.executable, '-X', 'utf8', str(script), operation,
                                      '--agent', agent, '--session', session, *args], cwd=root,
                                     capture_output=True, encoding='utf-8', timeout=15)
            check(operation + '_exit', process.returncode == 0)
            return json.loads(process.stdout)

        cli('init')
        codex = json.loads((ROOT / '.codex/hooks.json').read_text('utf-8'))['hooks']
        workbuddy = json.loads((ROOT / '.codebuddy/settings.json').read_text('utf-8'))['hooks']
        git = Path(shutil.which('git')).resolve()
        bash = git.parents[1] / 'bin/bash.exe'
        check('git_bash_available', bash.is_file())

        def hook(agent, event, session):
            source = (codex if agent == 'codex' else workbuddy)[event][-1]['hooks'][0]
            command = source.get('commandWindows', source['command'])
            prefix, encoded = command.rsplit(' ', 1)
            body = base64.b64decode(encoded).decode('utf-16le')
            body = body.replace(str(ROOT / 'setup/collaboration/project_sync.py'), str(script))
            command = prefix + ' ' + base64.b64encode(body.encode('utf-16le')).decode()
            if agent == 'codex':
                invocation = ['cmd.exe', '/d', '/c', command]
            else:
                invocation = [str(bash), '-c', command]
            payload = dict(cwd=str(root), session_id=session, hook_event_name=event,
                           tool_name='Write', tool_input=dict(file_path=str(root / 'README.md'), content='fixture'))
            started = time.perf_counter()
            process = subprocess.run(invocation, cwd=root, input=json.dumps(payload, ensure_ascii=False).encode(), capture_output=True, timeout=15)
            stderr = process.stderr.decode('utf-8', 'replace')
            if process.returncode:
                raise AssertionError(agent + '_' + event + ': ' + stderr[-3000:])
            check(agent + '_' + event + '_exit', process.returncode == 0)
            result = json.loads(process.stdout.decode('utf-8-sig'))
            runs.append(dict(agent=agent, event=event, seconds=round(time.perf_counter()-started, 4)))
            return result

        def sync_ack(agent, session):
            result = hook(agent, 'SessionStart', session)
            context = json.loads(result['hookSpecificOutput']['additionalContext'])
            report = json.loads(Path(context['report']).read_text('utf-8'))
            cli('ack', agent, session, '--token', context['ack_token'])
            return report

        sync_ack('codex', 'c1')
        hook('codex', 'PreToolUse', 'c1')
        (root / 'README.md').write_text('codex fixture edit', 'utf-8')
        blocked = hook('workbuddy', 'PreToolUse', 'w1')
        check('other_editor_blocked', blocked['hookSpecificOutput']['permissionDecision'] == 'deny')
        hook('codex', 'Stop', 'c1')
        report = sync_ack('workbuddy', 'w1')
        check('workbuddy_receives_codex_actual_hash', any(e['actor'] == 'codex' and e['kind'] == 'automatic_handoff' and e['changes'][0]['path'] == 'README.md' for e in report['events'] if e['changes']))
        hook('workbuddy', 'PreToolUse', 'w1')
        (root / 'README.md').write_text('workbuddy fixture edit', 'utf-8')
        hook('workbuddy', 'Stop', 'w1')
        report = sync_ack('codex', 'c1')
        check('codex_receives_workbuddy_actual_hash', any(e['actor'] == 'workbuddy' and e['kind'] == 'automatic_handoff' and e['changes'][0]['path'] == 'README.md' for e in report['events'] if e['changes']))
        check('lock_released_after_both_stops', cli('status')['lease'] is None)

        # A sender process -> installed receiver adapter -> explicit receiver ACK
        # -> reply process -> installed reverse adapter. Sessions remain synthetic.
        def send(agent, session, recipient, key, reply_to=None):
            path = root / '.collab' / (key + '.json')
            note = dict(key=key, summary='Realtime fixture message ' + key)
            if reply_to is not None:
                note['reply_to'] = reply_to
            path.write_text(json.dumps(note), 'utf-8')
            return cli('message', agent, session, '--to', recipient, '--note-file', str(path))

        # WorkBuddy has an older unread Codex handoff, then a targeted message.
        outbound = send('codex', 'c1', 'workbuddy', 'challenge')
        notice = hook('workbuddy', 'PostToolUse', 'w1')
        brief = json.loads(notice['hookSpecificOutput']['additionalContext'])
        check('active_workbuddy_receives_message', outbound['message_id'] in [e['id'] for e in brief['notifications']])
        check('notification_not_read_receipt', cli('status')['messages'][0]['acknowledged_by'] == [])
        check('notification_not_repeated', hook('workbuddy', 'PostToolUse', 'w1') == {})
        report = sync_ack('workbuddy', 'w1')
        check('workbuddy_reads_full_message', any(e['id'] == outbound['message_id'] for e in report['events']))
        check('workbuddy_explicit_read_receipt', 'workbuddy:w1' in cli('status')['messages'][0]['acknowledged_by'])
        reply = send('workbuddy', 'w1', 'codex', 'reply', outbound['message_id'])
        notice = hook('codex', 'PostToolUse', 'c1')
        brief = json.loads(notice['hookSpecificOutput']['additionalContext'])
        check('active_codex_receives_reply', reply['message_id'] in [e['id'] for e in brief['notifications']])
        report = sync_ack('codex', 'c1')
        check('reply_correlates_to_actual_message', any(e['details'].get('reply_to') == outbound['message_id'] for e in report['events']))

        # No polling loop in production: this bounded sample measures startup and
        # empty post-tool overhead using real Windows/Git Bash wrappers.
        for agent, session in [('codex', 'c1'), ('workbuddy', 'w1')]:
            for unused in range(3):
                hook(agent, 'PostToolUse', session)

        # Real independent processes competing for a shared SQLite lease.
        for agent, session in [('codex', 'race1'), ('workbuddy', 'race2')]:
            report = cli('sync', agent, session)
            cli('ack', agent, session, '--token', report['ack_token'])
        workers = [subprocess.Popen([sys.executable, '-X', 'utf8', str(script), 'begin', '--agent', agent, '--session', session],
                                   cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                   for agent, session in [('codex', 'race1'), ('workbuddy', 'race2')]]
        results = [json.loads(p.communicate(timeout=15)[0]) for p in workers]
        check('two_processes_one_edit_lease', sorted(r['status'] for r in results) == ['blocked', 'editing'])
        owner = cli('status')['lease']['owner']; agent, session = owner.split(':', 1)
        cli('finish', agent, session)
        report = dict(status='PASS', checks=checks, runs=runs,
                      post_tool_timing={agent: dict(samples=len(samples), median_seconds=round(statistics.median(samples), 4), max_seconds=max(samples))
                                        for agent in ('codex', 'workbuddy')
                                        if (samples := [r['seconds'] for r in runs if r['agent'] == agent and r['event'] == 'PostToolUse'])},
                      actual_host_session_tested=False, scope='real cmd.exe/Git Bash command adapters and SQLite CLI; synthetic sessions only')
        (OUT / 'hook_e2e.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        print(json.dumps(dict(status='PASS', checks=len(checks), scope=report['scope']), ensure_ascii=False))


if __name__ == '__main__':
    main()
