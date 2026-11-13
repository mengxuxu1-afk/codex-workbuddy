"""Synthetic HTTP/registry fixtures, not evidence of native model execution."""
import json
from pathlib import Path
import tempfile
import time
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx
import workbuddy_local as local

SESSION = '22222222-2222-4222-8222-222222222222'
PID = 1234


class LocalGateway(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.work/collaboration_20261110/native_tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = self.root / 'sessions'
        self.registry.mkdir()
        self.value = dict(sessionId=SESSION, cwd=str(self.root), lastHeartbeat=time.time()*1000,
                          endpoint='http://127.0.0.1:12345', pid=PID)
        self.write()
        self.endpoint = dict(endpoint=self.value['endpoint'], session=SESSION,
                             root=str(self.root), pid=PID)

    def write(self):
        (self.registry/'one.json').write_text(json.dumps(self.value), encoding='utf-8')

    def test_only_bound_project_session_is_discovered(self):
        self.assertEqual(local.discover(self.root, SESSION, self.registry), self.endpoint)
        self.value['cwd'] = str(self.root.parent); self.write()
        with self.assertRaises(local.LocalBlocked): local.discover(self.root, SESSION, self.registry)

    def test_heartbeat_allows_three_publish_intervals_but_never_future_or_stale(self):
        with patch.object(local.time, 'time', return_value=1000):
            for age in [0, 30001, 60000, 90000]:
                self.value['lastHeartbeat'] = 1000000-age; self.write()
                self.assertEqual(local.discover(self.root, SESSION, self.registry)['session'], SESSION)
            for age in [-1, 90001]:
                self.value['lastHeartbeat'] = 1000000-age; self.write()
                with self.assertRaises(local.LocalBlocked) as caught:
                    local.discover(self.root, SESSION, self.registry)
                self.assertEqual(caught.exception.code, 'session_stale')

    def test_acp_registry_metadata_uses_only_existing_live_and_reply_http_routes(self):
        self.value['endpoint'] += '/api/v1/acp'; self.write()
        endpoint = local.discover(self.root, SESSION, self.registry)
        self.assertEqual(endpoint['endpoint'], self.endpoint['endpoint'])
        calls=[]
        def handler(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json={'sessionId': SESSION})
        connection=local.ReplyConnection(dict(endpoint, executable='WorkBuddy.exe'),
            client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(local, 'process_gateway_password', return_value='fixture'):
            with connection: pass
        self.assertEqual(calls, [('GET', '/api/v1/sessions/live')])
        for suffix in ['/api/v1/acp/', '/api/v1/acp/other', '/api/v1/sessions/history']:
            self.value['endpoint'] = self.endpoint['endpoint'] + suffix; self.write()
            with self.assertRaises(local.LocalBlocked): local.discover(self.root, SESSION, self.registry)

    def test_missing_and_invalid_bound_records_have_distinct_safe_diagnostics(self):
        self.value['sessionId']='foreign'; self.write()
        with self.assertRaises(local.LocalBlocked) as caught:
            local.discover(self.root, SESSION, self.registry)
        self.assertEqual(caught.exception.code, 'session_not_registered')
        self.value['sessionId']=SESSION; self.value['endpoint']=None; self.write()
        with self.assertRaises(local.LocalBlocked) as caught:
            local.discover(self.root, SESSION, self.registry)
        self.assertEqual(caught.exception.code, 'registry_invalid')

    def test_missing_record_never_reads_process_or_probes_ports(self):
        home=self.root/'home'; registry=home/'.workbuddy/sessions'; registry.mkdir(parents=True)
        config=dict(workbuddy_transport='native_reply', workbuddy_local_registry_authorized=True,
                    workbuddy_process_gateway_authorized=True, workbuddy_executable='WorkBuddy.exe',
                    sessions=dict(workbuddy=SESSION))
        with patch.object(Path, 'home', return_value=home), \
             patch.object(local, 'process_gateway_password') as password, \
             patch.object(local.httpx, 'Client') as client:
            with self.assertRaises(local.LocalBlocked) as caught: local._preflight(self.root, config)
            self.assertEqual(caught.exception.code, 'session_not_registered')
            password.assert_not_called();client.assert_not_called()

    def test_safe_failure_code_survives_owned_worker_process(self):
        original=subprocess.Popen
        def fixture_worker(*args, **kwargs):
            return original([sys.executable,'-c',
                'import json,sys; sys.stdin.buffer.read(); print(json.dumps({"error_code":"session_not_registered"})); sys.exit(2)'], **kwargs)
        with patch.object(local.subprocess,'Popen',side_effect=fixture_worker):
            with self.assertRaises(local.LocalBlocked) as caught:local.run_worker({'action':'preflight'})
        self.assertEqual(caught.exception.code, 'session_not_registered')
        self.assertEqual(str(caught.exception), local.FAILURE_MESSAGES['session_not_registered'])

    def test_worker_entry_never_serializes_arbitrary_exception_or_credentials(self):
        payload=dict(action='preflight',root=str(self.root),session=SESSION,
                     registry_authorized=False,process_authorized=False,transport='native_reply',
                     executable='secret-sentinel-not-an-executable')
        process=subprocess.run([sys.executable,'-X','utf8',local.__file__,'--worker'],
            input=json.dumps(payload).encode(),capture_output=True,timeout=10)
        self.assertEqual(process.returncode,2)
        self.assertEqual(json.loads(process.stdout),{'error_code':'local_unavailable'})
        self.assertNotIn(b'secret-sentinel',process.stdout+process.stderr)

    def test_process_discovery_requires_real_positive_pid(self):
        for value in [None, True, 0, -1, '1234']:
            with self.subTest(pid=repr(value)):
                self.value['pid'] = value; self.write()
                with self.assertRaises(local.LocalBlocked): local.discover(self.root, SESSION, self.registry)
        self.value['pid'] = PID; self.write()
        self.assertEqual(local.discover(self.root, SESSION, self.registry)['pid'], PID)

    def test_password_selector_returns_only_authorized_variable(self):
        def extract(value):
            raw = value.encode('utf-16-le')
            return local.read_gateway_password(lambda offset: int.from_bytes(raw[offset:offset+2], 'little'), len(raw))
        self.assertEqual(extract('OTHER=ignored\0CODEBUDDY_GATEWAY_PASSWORD=fixture\0SECRET=ignored\0'), 'fixture')
        for value in ['OTHER=ignored\0', 'CODEBUDDY_GATEWAY_PASSWORD=\0',
                      'CODEBUDDY_GATEWAY_PASSWORD=' + 'x' * 4097]:
            with self.assertRaises(local.LocalBlocked): extract(value)

    def test_process_authorization_required_before_worker_or_registry(self):
        config = dict(workbuddy_transport='native_reply', workbuddy_local_registry_authorized=True,
                      sessions=dict(workbuddy=SESSION))
        with patch.object(local, 'run_worker') as worker, patch.object(local, 'discover') as discover:
            with self.assertRaises(local.LocalBlocked): local.preflight(self.root, config)
            with self.assertRaises(local.LocalBlocked): local._preflight(self.root, config)
        self.assertFalse(worker.called)
        self.assertFalse(discover.called)

    def test_missing_authorization_never_reads_process_or_network(self):
        base = dict(workbuddy_transport='native_reply',
                    workbuddy_local_registry_authorized=True,
                    workbuddy_process_gateway_authorized=True,
                    workbuddy_executable='WorkBuddy.exe', sessions=dict(workbuddy=SESSION))
        for field in ['workbuddy_local_registry_authorized', 'workbuddy_process_gateway_authorized']:
            with self.subTest(field=field):
                config = dict(base); config.pop(field)
                with patch.object(local, 'process_gateway_password') as password, \
                     patch.object(local, 'discover') as discover, \
                     patch.object(local.httpx, 'Client') as client:
                    with self.assertRaises(local.LocalBlocked): local._preflight(self.root, config)
                    password.assert_not_called()
                    discover.assert_not_called()
                    client.assert_not_called()

    def test_old_or_unknown_transport_blocked_before_process_or_network(self):
        for transport in ['local_gateway', 'disabled', 'oauth', None, 42]:
            with self.subTest(transport=repr(transport)):
                config = dict(workbuddy_transport=transport,
                              workbuddy_local_registry_authorized=True,
                              workbuddy_process_gateway_authorized=True,
                              workbuddy_executable='WorkBuddy.exe', sessions=dict(workbuddy=SESSION))
                with patch.object(local, 'process_gateway_password') as password, \
                     patch.object(local, 'discover') as discover, \
                     patch.object(local, 'run_worker') as worker, \
                     patch.object(local.httpx, 'Client') as client:
                    with self.assertRaises(local.LocalBlocked): local.preflight(self.root, config)
                    with self.assertRaises(local.LocalBlocked): local._preflight(self.root, config)
                    with self.assertRaises(local.LocalBlocked):
                        local._submit(dict(self.endpoint, transport=transport, executable='WorkBuddy.exe'), 'must not send')
                    password.assert_not_called()
                    discover.assert_not_called()
                    worker.assert_not_called()
                    client.assert_not_called()

    def test_worker_payload_carries_real_authorization_flags(self):
        config = dict(workbuddy_transport='native_reply',
                      workbuddy_local_registry_authorized=True,
                      workbuddy_process_gateway_authorized=True,
                      workbuddy_executable='WorkBuddy.exe', sessions=dict(workbuddy=SESSION))
        with patch.object(local, 'run_worker', return_value=self.endpoint) as worker:
            local.preflight(self.root, config)
        payload = worker.call_args.args[0]
        self.assertEqual(payload['transport'], 'native_reply')
        self.assertIs(payload['registry_authorized'], True)
        self.assertIs(payload['process_authorized'], True)

    def test_authenticated_reply_uses_only_original_live_session(self):
        calls = []; released = []
        def handler(request):
            calls.append((request.method, request.url.path))
            self.assertEqual(request.headers['Authorization'], 'Bearer fixture')
            if request.method == 'GET':
                return httpx.Response(200, json={'data': {'sessionId': SESSION, 'writerOccupied': True}})
            self.assertEqual(json.loads(request.content), {'text': 'actual notification'})
            self.assertEqual(released, [True])
            return httpx.Response(200, json={'data': {'delivered': True}})
        endpoint = dict(self.endpoint, executable='WorkBuddy.exe')
        connection = local.ReplyConnection(endpoint, client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(local, 'process_gateway_password', return_value='fixture'):
            with connection:
                self.assertTrue(connection.send('actual notification', accepted=lambda: released.append(True)).startswith('native-reply:'))
        self.assertEqual(calls, [('GET', '/api/v1/sessions/live'), ('POST', f'/api/v1/sessions/{SESSION}/reply')])
        self.assertEqual(connection.headers, {})
        self.assertTrue(connection.client.is_closed)

    def test_wrong_live_session_and_redirect_never_post(self):
        for response in [httpx.Response(200, json={'sessionId': 'foreign'}),
                         httpx.Response(302, headers={'Location': 'http://foreign.invalid/'}),
                         httpx.Response(401, json={'error': 'fixture'})]:
            calls = []
            def handler(request):
                calls.append(request.method)
                return response
            connection = local.ReplyConnection(dict(self.endpoint, executable='WorkBuddy.exe'),
                client=httpx.Client(transport=httpx.MockTransport(handler)))
            with patch.object(local, 'process_gateway_password', return_value='fixture'):
                with self.assertRaises(local.LocalBlocked):
                    with connection: connection.send('must not send')
            self.assertEqual(calls, ['GET'])
            self.assertTrue(connection.client.is_closed)

    def test_history_and_session_switch_paths_rejected_before_network(self):
        # Migrated from the retired HTTP adapter: no history/new/load/resume path.
        handler_calls = []
        def handler(request):
            handler_calls.append(request.url.path)
            return httpx.Response(200, json={})
        connection = local.ReplyConnection(dict(self.endpoint, executable='WorkBuddy.exe'),
            client=httpx.Client(transport=httpx.MockTransport(handler)))
        for method, path in [('GET', '/api/v1/sessions/history'),
                             ('POST', '/api/v1/sessions/new'),
                             ('POST', f'/api/v1/sessions/{SESSION}/load'),
                             ('POST', f'/api/v1/sessions/{SESSION}/prompt')]:
            with self.subTest(path=path):
                with self.assertRaises(local.LocalBlocked): connection.request(method, path)
        self.assertEqual(handler_calls, [])
        connection.client.close()

    def test_reply_response_is_bounded_to_64kib(self):
        # Migrated from the retired ACP connect/SSE size guard: the surviving
        # live/reply JSON path keeps the same strict 64KiB bound.
        def handler(request):
            return httpx.Response(200, content=b'x' * 65537)
        connection = local.ReplyConnection(dict(self.endpoint, executable='WorkBuddy.exe'),
            client=httpx.Client(transport=httpx.MockTransport(handler)))
        with self.assertRaises(local.LocalBlocked):
            connection.request('GET', '/api/v1/sessions/live')
        connection.client.close()

    def test_reply_submission_rechecks_pid_and_binding_before_auth(self):
        endpoint = dict(self.endpoint, executable='WorkBuddy.exe', transport='native_reply')
        with patch.object(local, 'discover', return_value=dict(self.endpoint, pid=5678)), \
             patch.object(local, 'ReplyConnection') as connection:
            with self.assertRaises(local.LocalBlocked): local._submit(endpoint, 'must not send')
        self.assertFalse(connection.called)

    def test_valid_native_dispatch_and_reply_simulation(self):
        # End-to-end simulation of the surviving route: preflight -> rediscover
        # -> live check -> reply. No real desktop process is contacted.
        config = dict(workbuddy_transport='native_reply',
                      workbuddy_local_registry_authorized=True,
                      workbuddy_process_gateway_authorized=True,
                      workbuddy_executable='WorkBuddy.exe', sessions=dict(workbuddy=SESSION))
        home = self.root / 'home'
        registry = home / '.workbuddy/sessions'
        registry.mkdir(parents=True)
        (registry/'one.json').write_text(json.dumps(self.value), encoding='utf-8')
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path))
            self.assertEqual(request.headers['Authorization'], 'Bearer fixture')
            if request.method == 'GET':
                return httpx.Response(200, json={'data': {'sessionId': SESSION}})
            return httpx.Response(200, json={'data': {'delivered': True}})
        original_client = httpx.Client
        def new_client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler))
        with patch.object(Path, 'home', return_value=home), \
             patch.object(local, 'process_gateway_password', return_value='fixture'), \
             patch.object(local.httpx, 'Client', side_effect=new_client):
            endpoint = local._preflight(self.root, config)
            self.assertEqual(endpoint['transport'], 'native_reply')
            self.assertTrue(local._submit(endpoint, 'fixture notification').startswith('native-reply:'))
        self.assertEqual(calls, [('GET', '/api/v1/sessions/live'),
                                 ('GET', '/api/v1/sessions/live'),
                                 ('POST', f'/api/v1/sessions/{SESSION}/reply')])

    def test_foreign_session_stale_oversize_and_ambiguous_records_block(self):
        for field, value in [('sessionId', 'foreign'), ('lastHeartbeat', 1), ('lastHeartbeat', True),
                             ('pid', 0), ('pid', True)]:
            old = self.value[field]; self.value[field] = value; self.write()
            with self.assertRaises(local.LocalBlocked): local.discover(self.root, SESSION, self.registry)
            self.value[field] = old
        self.write()
        (self.registry/'two.json').write_text(json.dumps(self.value), encoding='utf-8')
        with self.assertRaises(local.LocalBlocked): local.discover(self.root, SESSION, self.registry)

    def test_remote_credentials_redirects_and_bad_ports_not_accepted(self):
        for endpoint in ['https://127.0.0.1:12345', 'http://remote.invalid:12345',
                         'http://localhost:12345', 'http://user:pass@127.0.0.1:12345',
                         'http://127.0.0.1:12345/?token=value', 'http://127.0.0.1:bad']:
            self.value['endpoint'] = endpoint; self.write()
            with self.assertRaises(local.LocalBlocked): local.discover(self.root, SESSION, self.registry)

    def test_registry_access_requires_explicit_project_authorization(self):
        with patch.object(local, 'discover') as discover:
            with self.assertRaises(local.LocalBlocked): local.preflight(self.root, {})
            discover.assert_not_called()

    def test_native_supervisor_terminates_and_reaps_silent_owned_child(self):
        original = subprocess.Popen
        children = []
        def silent_worker(*args, **kwargs):
            child = original([sys.executable,'-c','import time; time.sleep(60)'], **kwargs)
            children.append(child)
            return child
        start=time.monotonic()
        with patch.object(local.subprocess,'Popen',side_effect=silent_worker), \
                patch.object(local,'WORKER_BUDGETS',{'submit':0.15}),patch.object(local,'WORKER_GRACE',0.1):
            with self.assertRaises(local.LocalBlocked):local.run_worker({'action':'submit'})
        self.assertLess(time.monotonic()-start,3)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stdin.closed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
