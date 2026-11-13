"""Behavioral regression tests. Host/model are simulated; not live acceptance."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch, AsyncMock
from types import MethodType
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_sync import Collaboration as Service, digest, snapshot
from workbuddy_acp_bridge.acp_client import WorkBuddyAcpClient, AcpError, AcpRunResult
from workbuddy_acp_bridge.gateway import GatewayDiscovery, GatewayEndpoint, GatewayDiscoveryError
from workbuddy_acp_bridge.project import ProjectRuntime, ProjectBlocked, PermissionPolicy, atomic_json

BASE = Path(__file__).resolve().parents[2] / '.work/collaboration_20261111/acp_tests'
BASE.mkdir(parents=True, exist_ok=True)
CODEX = '00000000-0000-4000-8000-000000000002'
DESKTOP = '00000000-0000-4000-8000-000000000001'
HIDDEN = 'd466fdd0-61cb-44a5-a323-bdb77ce9f4ad'


class ProtocolClient(WorkBuddyAcpClient):
    def __init__(self):
        self.calls = []
        super().__init__(GatewayEndpoint('http://127.0.0.1:1', 1, 0, Path('fixture')))

    async def connect(self):
        self._connection_id = 'fixture'

    async def request(self, method, params, **kw):
        self.calls.append((method, params))
        if method == 'session/new':
            return {'sessionId':HIDDEN}
        return {'stopReason':'end_turn'}


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_rounds_preserve_context_and_skip_clear(self):
        c = ProtocolClient()
        try:
            first = await c.run('first', cwd=BASE)
            second = await c.run('second', cwd=BASE, session_id=first.session_id)
            self.assertEqual(second.session_id, HIDDEN)
            self.assertTrue(second.session_reused)
            prompts = [p['prompt'][0]['text'] for m,p in c.calls if m == 'session/prompt']
            self.assertEqual(prompts, ['first','second'])
            self.assertEqual(sum(m == 'session/new' for m,p in c.calls), 1)
            self.assertFalse(any(m in {'session/set_model','session/set_config_option'} for m,p in c.calls))
        finally:
            await c._http.aclose()

    async def test_permission_requires_scope_even_with_allow_once(self):
        c = ProtocolClient()
        try:
            c.permission_mode = 'allow_once'
            params={'options':[{'kind':'allow_once','optionId':'yes'}]}
            self.assertEqual(c._permission_result(params)['outcome']['outcome'], 'cancelled')
            c.permission_handler=lambda p: True
            self.assertEqual(c._permission_result(params)['outcome']['optionId'],'yes')
        finally:
            await c._http.aclose()


class StreamDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_post_uses_request_budget_and_releases_pending(self):
        seen=[]
        async def handler(request):
            if request.method=='DELETE':return httpx.Response(200)
            seen.append(request.extensions['timeout']['read'])
            body=json.loads(request.content)
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':{'stopReason':'end_turn'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            c=WorkBuddyAcpClient(GatewayEndpoint('http://127.0.0.1:1',1,0,Path('fixture')),http_client=http)
            c._connection_id='fixture'
            try:
                c.last_transport_error={'phase':'sse','error_type':'PreviousError'}
                result=await c.request('session/prompt',{'sessionId':HIDDEN},timeout_seconds=77)
                self.assertEqual(result['stopReason'],'end_turn')
                self.assertIsNone(c.last_transport_error)
                self.assertEqual(seen,[77])
                self.assertFalse(c._pending)
                self.assertFalse(c._post_tasks)
            finally:await c.close()

    async def test_absolute_timeout_cancels_owned_post_without_replaying(self):
        methods=[]
        async def handler(request):
            if request.method=='DELETE':return httpx.Response(200)
            method=json.loads(request.content).get('method');methods.append(method)
            if method=='session/prompt':await asyncio.Event().wait()
            return httpx.Response(200,json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            c=WorkBuddyAcpClient(GatewayEndpoint('http://127.0.0.1:1',1,0,Path('fixture')),http_client=http)
            c._connection_id='fixture'
            try:
                with self.assertRaises(AcpError):
                    await c.request('session/prompt',{'sessionId':HIDDEN},timeout_seconds=.02)
                self.assertEqual(methods,['session/prompt','session/cancel'])
                self.assertEqual(c.last_transport_error,{'phase':'deadline','error_type':'TimeoutError'})
                self.assertFalse(c._pending)
                self.assertFalse(c._post_tasks)
            finally:await c.close()

    async def test_idle_receive_stream_has_no_short_read_timeout(self):
        seen=[]
        class QuietStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b''
        async def handler(request):
            if request.method=='DELETE':return httpx.Response(200)
            seen.append(request.extensions['timeout']['read'])
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=QuietStream())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            c=WorkBuddyAcpClient(GatewayEndpoint('http://127.0.0.1:1',1,0,Path('fixture')),http_client=http)
            c._connection_id='fixture';c._listener_task=asyncio.create_task(c._listen_sse())
            try:
                await asyncio.sleep(.02)
                self.assertEqual(seen,[None])
                self.assertFalse(c._listener_task.done())
            finally:await c.close()

    async def test_sse_reconnect_counts_consecutive_failures(self):
        calls=[]
        class ValidThenBroken(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"jsonrpc":"2.0","method":"session/update","params":{}}\n\n'
                raise httpx.ReadError('simulated disconnect')
        async def handler(request):
            if request.method=='DELETE':return httpx.Response(200)
            calls.append(1)
            if len(calls)==3:return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=ValidThenBroken())
            raise httpx.ReadError('simulated disconnect')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            c=WorkBuddyAcpClient(GatewayEndpoint('http://127.0.0.1:1',1,0,Path('fixture')),http_client=http)
            c._connection_id='fixture'
            try:
                with patch('workbuddy_acp_bridge.acp_client.asyncio.sleep',new=AsyncMock()):await c._listen_sse()
                self.assertEqual(len(calls),5)
                self.assertEqual(c._sse_events,1)
            finally:await c.close()


class FakeClient:
    instances = []
    fail_run = False

    def __init__(self, gateway, **kw):
        self.gateway=gateway
        self._listener_task=None
        self._loaded_sessions=set()
        self._session_modes={}
        self.calls=[]
        self.closed=False
        self.permission_handler=None
        FakeClient.instances.append(self)

    async def connect(self):
        self.calls.append('initialize')

    async def close(self):
        self.closed=True

    async def request(self, method, params, **kw):
        self.calls.append(method)
        if method in {'session/new','session/load'}:return {'sessionId':HIDDEN,'modes':{'currentModeId':'default'}}
        if method=='session/set_mode':return {}
        return {}

    def _extract_session_id(self, r):return r['sessionId']

    async def run(self, prompt, **kw):
        self.calls.append('prompt')
        if self.fail_run:raise AcpError('simulated disconnected unknown submission')
        return AcpRunResult(kw['session_id'], 'simulated execution result', 'auto', 'disabled')

    async def cancel(self, session):self.calls.append('cancel')


class FakeDiscovery:
    def __init__(self):self.pid=42
    def discover_host(self):
        return GatewayEndpoint('http://127.0.0.1:7777',self.pid,time.time()*1000,Path('fixture'))


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=BASE)
        self.root=Path(self.tmp.name)
        self.root.joinpath('.collab').mkdir()
        self.config={'root':str(self.root),'enabled':True,'sessions':{'codex':CODEX,'workbuddy':DESKTOP},
                     'workbuddy_transport':'native_reply','workbuddy_route_verified':False}
        atomic_json(self.root/'.collab/desktop.json',self.config)
        Service(self.root).init()
        FakeClient.instances=[]
        FakeClient.fail_run=False
        self.discovery=FakeDiscovery()
        self.runtime=ProjectRuntime(self.root, discovery=self.discovery,client_factory=FakeClient)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def pair(self):return self.runtime.pair(CODEX,'video-edit')

    def test_pair_is_project_local_and_keeps_desktop_binding(self):
        self.assertEqual(self.pair()['session_id'], HIDDEN)
        conf=json.loads((self.root/'.collab/desktop.json').read_text('utf-8'))
        self.assertEqual(conf['native_workbuddy_session'], DESKTOP)
        self.assertEqual(conf['sessions']['workbuddy'],HIDDEN)
        self.assertFalse(conf['workbuddy_route_verified'])
        self.assertEqual(self.runtime.pair(CODEX,'video-edit')['status'],'already_paired')
        self.assertEqual(sum(c.calls.count('session/new') for c in FakeClient.instances),1)
        self.assertTrue(self.runtime.state_path.is_relative_to(self.root))

    def test_wrong_identity_blocks_before_connection(self):
        with self.assertRaises(ProjectBlocked):self.runtime.pair(DESKTOP,'video-edit')
        self.assertEqual(FakeClient.instances,[])

    def test_missing_host_does_not_poison_session_creation(self):
        with patch.object(self.discovery, 'discover_host', side_effect=GatewayDiscoveryError('Host not registered')):
            with self.assertRaises(GatewayDiscoveryError):self.pair()
        self.assertIsNone(self.runtime.read_state())
        self.assertEqual(FakeClient.instances,[])
        self.assertEqual(self.pair()['session_id'],HIDDEN)

    def test_foreign_state_blocks(self):
        self.runtime.save_state({'project_id':'other','project_root':str(self.root)})
        with self.assertRaises(ProjectBlocked):self.runtime.read_state()

    def test_host_restart_reconnects_without_new_session(self):
        self.pair()
        first=FakeClient.instances[-1]
        self.discovery.pid=43
        self.assertTrue(self.runtime.status()['gateway_connected'])
        self.assertTrue(first.closed)
        self.assertEqual(self.runtime.read_state()['session_id'],HIDDEN)
        self.assertNotIn('session/new',FakeClient.instances[-1].calls)

    def test_unknown_submission_persists_and_cannot_replay(self):
        self.pair()
        event=Service(self.root).message('codex',CODEX,'workbuddy',
            {'key':'task.acp-acceptance.unknown','summary':'test unknown'})['message_id']
        self.runtime.set_policy(CODEX,event,['.work/acp_acceptance'],[],2)
        FakeClient.fail_run=True
        with self.assertRaises(AcpError):self.runtime.submit('real-envelope-fixture',event,HIDDEN)
        s=self.runtime.read_state()
        self.assertEqual((s['status'],s['active_event']),('task_unknown',event))
        self.assertEqual(s['last_error']['error_type'],'AcpError')
        self.assertIsInstance(s['last_error']['elapsed_seconds'],float)
        self.assertNotIn('message',s['last_error'])
        with self.assertRaises(ProjectBlocked):self.runtime.submit('again',event,HIDDEN)
        self.assertEqual(FakeClient.instances[-1].calls.count('prompt'),1)

    def test_cancel_never_claims_confirmed_stop(self):
        self.pair()
        s=self.runtime.read_state();s['active_event']=1;self.runtime.save_state(s)
        result=self.runtime.cancel(CODEX)
        self.assertEqual(result['status'],'cancel_requested')
        self.assertFalse(result['stopped_confirmed'])

    def unknown_for_recovery(self):
        self.pair()
        service=Service(self.root)
        event=service.message('codex',CODEX,'workbuddy',{'key':'task.acp-acceptance.recovery','summary':'original unknown'})['message_id']
        state=self.runtime.read_state();state.update(status='task_unknown',active_event=event);self.runtime.save_state(state)
        with service.transaction() as db:
            db.execute('CREATE TABLE desktop_deliveries(event_id INTEGER PRIMARY KEY,status TEXT,receipt TEXT,time REAL)')
            db.execute('INSERT INTO desktop_deliveries VALUES(?,?,?,?)',(event,'unknown_no_resubmit',None,time.time()-5))
        while True:
            page=service.sync('codex',CODEX);service.ack('codex',CODEX,page['ack_token'])
            if not page['more']:break
        service.begin('codex',CODEX)
        proof=dict(session_id=HIDDEN,pid=42,busy=False,status='idle',mode='default',last_completed_at=time.time()*1000,checked_at=time.time())
        return service,event,proof

    def test_recovery_preserves_unknown_ledger_and_allows_independent_scoped_task(self):
        service,event,proof=self.unknown_for_recovery()
        with patch.object(self.runtime,'_bound_runtime_status',new=AsyncMock(return_value=proof)):
            recovered=self.runtime.recover(CODEX)
            self.assertTrue(recovered['stopped_confirmed'])
            self.assertFalse(recovered['task_outcome_verified'])
            state=self.runtime.read_state()
            self.assertIsNone(state['active_event']);self.assertEqual(state['last_unresolved_event'],event)
            config=json.loads((self.root/'.collab/desktop.json').read_text('utf-8'))
            self.assertFalse(config['workbuddy_route_verified'])
            self.assertTrue(service.acp_execution_ready(config))
            cleanup=service.message('codex',CODEX,'workbuddy',{'key':'task.bridge-cleanup.independent','summary':'new authorized scoped task'})['message_id']
            self.runtime.set_policy(CODEX,cleanup,['.work/cleanup'],[],10)
            service.finish('codex',CODEX,{'summary':'release for scoped task','tests':'simulated Host','remaining':''})
            with patch('workbuddy_acp_bridge.project.runtime_for',return_value=self.runtime):
                outcome=service.dispatch('codex',CODEX,cleanup)
                repeated=service.dispatch('codex',CODEX,event)
            self.assertEqual(outcome['status'],'sent_awaiting_ack')
            self.assertEqual(repeated['status'],'unknown_no_resubmit')
            self.assertTrue(repeated['reused'])
            with service.transaction() as db:
                self.assertEqual(db.execute('SELECT status FROM desktop_deliveries WHERE event_id=?',(event,)).fetchone()[0],'unknown_no_resubmit')
        self.assertEqual(FakeClient.instances[-1].calls.count('prompt'),1)

    def test_busy_recovery_does_not_release_unknown(self):
        service,event,proof=self.unknown_for_recovery();proof.update(busy=True,status='waiting_for_permission')
        with patch.object(self.runtime,'_bound_runtime_status',new=AsyncMock(return_value=proof)):
            out=self.runtime.recover(CODEX)
        self.assertEqual(out['status'],'remote_busy');self.assertFalse(out['stopped_confirmed'])
        self.assertEqual(self.runtime.read_state()['active_event'],event)
        self.assertNotIn('prompt',FakeClient.instances[-1].calls)

    def test_stale_completion_cannot_reconcile_new_unknown(self):
        service,event,proof=self.unknown_for_recovery()
        for bad in [1,float('nan'),float('inf')]:
            proof['last_completed_at']=bad
            with patch.object(self.runtime,'_bound_runtime_status',new=AsyncMock(return_value=proof)):
                with self.assertRaises(ProjectBlocked):self.runtime.recover(CODEX)
        self.assertEqual(self.runtime.read_state()['active_event'],event)

    def test_recovery_requires_lease_and_cannot_trust_edited_readiness(self):
        service,event,proof=self.unknown_for_recovery()
        with patch.object(self.runtime,'_bound_runtime_status',new=AsyncMock(return_value=proof)):
            self.runtime.recover(CODEX)
        state=self.runtime.read_state();state['execution_readiness']['evidence_hash']='forged';self.runtime.save_state(state)
        config=json.loads((self.root/'.collab/desktop.json').read_text('utf-8'))
        self.assertFalse(service.acp_execution_ready(config))
        service.finish('codex',CODEX,{'summary':'end fixture','tests':'fixture','remaining':''})
        with self.assertRaises(ProjectBlocked):self.runtime.recover(CODEX)

    def test_bound_status_requires_auth_before_network(self):
        with self.assertRaises(ProjectBlocked):
            self.runtime.call(self.runtime._bound_runtime_status(self.config,HIDDEN))
        self.assertEqual(FakeClient.instances,[])

    def test_exact_project_helper_help_is_read_only_not_general_shell(self):
        import shlex
        helper=self.root/'setup/collaboration/project_sync.py';helper.parent.mkdir(parents=True);helper.touch()
        command=shlex.join([sys.executable,'-X','utf8',str(helper),'receive','--help'])
        service=Service(self.root)
        self.assertTrue(service.helper_command({'command':command},'workbuddy',HIDDEN))
        for bad in [command+'; echo injected',command+' --event-id 1',command.replace(str(helper),'other.py')]:
            self.assertFalse(service.helper_command({'command':bad},'workbuddy',HIDDEN))

    def test_actual_grep_tool_is_project_read_only_and_rejects_private_or_foreign(self):
        policy=PermissionPolicy(self.root,['.work/cleanup'],[],HIDDEN)
        def request(path):return {'sessionId':HIDDEN,'toolCall':{'_meta':{'codebuddy.ai/toolName':'Grep'},'rawInput':{'path':str(path),'pattern':'notification','output_mode':'content'}}}
        source=self.root/'setup/collaboration/project_sync.py';source.parent.mkdir(parents=True);source.write_text('notification')
        self.assertTrue(policy(request(source)))
        self.assertFalse(policy(request(source.parent)))
        self.assertFalse(policy(request(self.root)))
        self.assertFalse(policy(request(self.root/'.collab/private')))
        self.assertFalse(policy(request(self.root.parent)))
        bad=request(self.root);bad['sessionId']=DESKTOP
        self.assertFalse(policy(bad))

    def test_scoped_cancelled_repair_keeps_partial_work_and_original_root(self):
        service,old,proof=self.unknown_for_recovery()
        with patch.object(self.runtime,'_bound_runtime_status',new=AsyncMock(return_value=proof)):
            self.runtime.recover(CODEX)
        parent=service.message('codex',CODEX,'workbuddy',{'key':'task.cleanup.cancelled','summary':'first scoped task'})['message_id']
        state=self.runtime.read_state();state['last_event']=parent;self.runtime.save_state(state)
        atomic_json(self.runtime.folder/f'result-{parent}.json',dict(session_id=HIDDEN,cwd=str(self.root),stop_reason='cancelled',permissions=[{'decision':'allow_once'},{'decision':'cancelled'}]))
        with service.transaction() as db:
            db.execute('INSERT INTO desktop_deliveries VALUES(?,?,?,?)',(parent,'sent_awaiting_ack',f'project-acp:{parent}:{HIDDEN}',time.time()))
        part=self.root/'.work/partial';part.parent.mkdir(exist_ok=True);part.write_text('preserve')
        repair=service.message('codex',CODEX,'workbuddy',{'key':'task.cleanup.repair','summary':'repair permission only','reply_to':parent})['message_id']
        with service.transaction() as db:
            details=json.loads(db.execute('SELECT details FROM events WHERE id=?',(repair,)).fetchone()[0])
            self.assertEqual(details['reply_to'],parent)
            db.execute("UPDATE desktop_deliveries SET status='unknown_no_resubmit' WHERE event_id=?",(parent,))
        self.assertEqual(part.read_text(),'preserve')
        with self.assertRaises(ValueError):service.message('codex',CODEX,'workbuddy',{'key':'task.cleanup.bad','summary':'must not use unknown','reply_to':parent})

    def test_bound_status_authenticates_only_bound_host_and_rejects_foreign_session(self):
        self.pair();self.discovery.executable=Path('fixture-workbuddy.exe')
        import workbuddy_local as local
        config=json.loads((self.root/'.collab/desktop.json').read_text('utf-8'))
        config.update(workbuddy_local_registry_authorized=True,workbuddy_process_gateway_authorized=True)
        atomic_json(self.root/'.collab/desktop.json',config)
        def handler(request):
            self.assertEqual(str(request.url),'http://127.0.0.1:7777/api/v1/status')
            self.assertEqual(request.headers.get('Authorization'),'Bearer temporary-fixture')
            return httpx.Response(200,json={'data':{'activeSessionId':DESKTOP,'busy':False,'runStatus':'idle'}})
        expected=dict(session=HIDDEN,codex=CODEX,pid=42,base_url='http://127.0.0.1:7777')
        with patch('workbuddy_local.process_gateway_password',return_value='temporary-fixture') as read:
            with self.assertRaises(local.LocalBlocked):
                local._project_runtime_status(self.root,expected,client=httpx.Client(transport=httpx.MockTransport(handler)),discovery=self.discovery)
            read.assert_called_once_with(42,self.discovery.executable)

    def test_bound_status_limits_remote_response_and_never_follows_redirect(self):
        self.pair();self.discovery.executable=Path('fixture-workbuddy.exe')
        import workbuddy_local as local
        config=json.loads((self.root/'.collab/desktop.json').read_text('utf-8'))
        config.update(workbuddy_local_registry_authorized=True,workbuddy_process_gateway_authorized=True)
        atomic_json(self.root/'.collab/desktop.json',config)
        expected=dict(session=HIDDEN,codex=CODEX,pid=42,base_url='http://127.0.0.1:7777')
        for status,body in [(302,b''),(200,b' '*65537)]:
            seen=[]
            def handler(request):
                seen.append(str(request.url));return httpx.Response(status,content=body,headers={'Location':'http://example.test'})
            with patch('workbuddy_local.process_gateway_password',return_value='temporary-fixture'):
                with self.assertRaises(local.LocalBlocked):
                    local._project_runtime_status(self.root,expected,client=httpx.Client(transport=httpx.MockTransport(handler)),discovery=self.discovery)
            self.assertEqual(len(seen),1)

    def test_recovery_readiness_rechecks_actual_host_pid_before_dispatch(self):
        service,event,proof=self.unknown_for_recovery()
        with patch.object(self.runtime,'_bound_runtime_status',new=AsyncMock(return_value=proof)):
            self.runtime.recover(CODEX)
        proof['pid']=999
        config=json.loads((self.root/'.collab/desktop.json').read_text('utf-8'))
        with patch.object(self.runtime,'_bound_runtime_status',new=AsyncMock(return_value=proof)):
            with self.assertRaises(ProjectBlocked):self.runtime.prepare(config)

    def test_mode_confirmation_uses_real_notification_not_response_field(self):
        self.pair()
        client=FakeClient.instances[-1]
        client._session_modes[HIDDEN]='bypassPermissions'
        async def request(c, method, params, **kw):
            c.calls.append(method)
            if method=='session/set_mode':
                c._session_modes[params['sessionId']]='default'
                return {}  # Actual Desktop 5.9.2 response contract.
            return {}
        client.request=MethodType(request,client)
        self.runtime.call(self.runtime._ensure_default(client,HIDDEN))
        self.assertEqual(client._session_modes[HIDDEN],'default')
        self.assertNotIn('prompt',client.calls)

    def test_unconfirmed_bypass_mode_blocks_before_prompt(self):
        self.pair()
        client=FakeClient.instances[-1]
        client._session_modes[HIDDEN]='bypassPermissions'
        async def request(c,method,params,**kw):
            return {'modes':{'currentModeId':'bypassPermissions'}} if method=='session/load' else {}
        client.request=MethodType(request,client)
        with self.assertRaises(ProjectBlocked):self.runtime.call(self.runtime._ensure_default(client,HIDDEN))
        self.assertNotIn('prompt',client.calls)

    def test_dispatch_receive_actual_file_change_and_linked_result(self):
        self.pair()
        service=Service(self.root)
        event=service.message('codex',CODEX,'workbuddy',
            {'key':'task.acp-acceptance.integration','summary':'Add project acceptance test fixture'})['message_id']
        self.runtime.set_policy(CODEX,event,['setup/collaboration/test_project_acp_acceptance.py'],[],10)
        client=FakeClient.instances[-1]
        async def model_stub(c,envelope,**kw):
            # Model/Host are simulated. SQLite, files, receive, leases and the
            # real dispatch/bridge/MCP consumer chain are exercised below.
            bootstrap=json.loads(envelope)
            notice=bootstrap['notification']
            self.assertEqual(bootstrap['assignment'],'Add project acceptance test fixture')
            self.assertEqual(bootstrap['task_policy']['session'],HIDDEN)
            self.assertEqual(bootstrap['task_policy']['allowed_paths'],['setup/collaboration/test_project_acp_acceptance.py'])
            self.assertIn('project_sync.py',bootstrap['task_policy']['collaboration_helper_prefix'])
            self.assertEqual(bootstrap['protocol']['event_id'],event)
            self.assertEqual(bootstrap['protocol']['receipt'],{'notification':notice})
            self.assertEqual(bootstrap['protocol']['result_template']['reply_to'],event)
            while True:
                page=service.sync('workbuddy',HIDDEN)
                service.ack('workbuddy',HIDDEN,page['ack_token'])
                if not page['more']:break
            service.receive('workbuddy',HIDDEN,event,{'notification':notice})
            self.assertEqual(service.begin('workbuddy',HIDDEN)['status'],'editing')
            p=self.root/'setup/collaboration/test_project_acp_acceptance.py'
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('def frame_count(seconds, fps):\n    return round(seconds * fps)\n',encoding='utf-8')
            service.finish('workbuddy',HIDDEN,{'summary':'simulated-model real file change','tests':'fixture','remaining':''})
            reply=service.message('workbuddy',HIDDEN,'codex',
                {'key':'result.acp-acceptance.integration','summary':'fixture written','reply_to':event})['message_id']
            return AcpRunResult(HIDDEN,f'result event {reply}','auto','disabled')
        client.run=MethodType(model_stub,client)
        from workbuddy_acp_bridge import server
        with patch.object(server,'binding',return_value=(self.runtime,CODEX)), \
             patch('workbuddy_acp_bridge.project.runtime_for',return_value=self.runtime):
            result=server.execute('workbuddy_run',{'event_id':event,
                'allowed_paths':['setup/collaboration/test_project_acp_acceptance.py'],
                'allowed_commands':[],'timeout_seconds':10})
        self.assertEqual(result['status'],'recipient_received')
        self.assertFalse(result['independently_accepted'])
        self.assertTrue(Path(result['evidence_path']).is_relative_to(self.root))
        self.assertIsNone(service.status()['lease'])
        self.assertTrue((self.root/'setup/collaboration/test_project_acp_acceptance.py').exists())
        with service.transaction() as db:
            reply=db.execute("SELECT details FROM events WHERE kind='message' AND actor='workbuddy'").fetchone()
            self.assertEqual(json.loads(reply['details'])['reply_to'],event)
        repeated=service.dispatch('codex',CODEX,event)
        self.assertTrue(repeated['reused'])
        self.assertEqual(repeated['status'],'recipient_received')

    def test_mcp_rejects_project_override_before_pair_or_prompt(self):
        from workbuddy_acp_bridge import server
        with patch.object(server,'binding',return_value=(self.runtime,CODEX)):
            with self.assertRaises(ProjectBlocked):
                server.execute('workbuddy_pair',{'confirm':'video-edit','cwd':'another-project'})
        self.assertIsNone(self.runtime.read_state())
        self.assertEqual(FakeClient.instances,[])

    def cancelled_assignment(self, reply_to=None, key='initial'):
        service=Service(self.root)
        details={'key':'task.acp-acceptance.cancel.'+key,'summary':'scoped permission cancellation'}
        if reply_to is not None:details['reply_to']=reply_to
        event=service.message('codex',CODEX,'workbuddy',details)['message_id']
        self.runtime.set_policy(CODEX,event,['.work/acp_acceptance'],[],10)
        async def cancelled(c,prompt,**kw):
            c.calls.append('prompt')
            return AcpRunResult(HIDDEN,'','auto','disabled',stop_reason='cancelled',
                permissions=[{'tool':'Bash','decision':'cancelled'}],cwd=str(self.root))
        FakeClient.instances[-1].run=MethodType(cancelled,FakeClient.instances[-1])
        with patch('workbuddy_acp_bridge.project.runtime_for',return_value=self.runtime):
            receipt=service.dispatch('codex',CODEX,event)
        self.assertEqual(receipt['status'],'sent_awaiting_ack')
        return event

    def test_cancelled_permission_repairs_keep_original_retry_budget(self):
        self.pair()
        parent=None
        for i in range(3):
            parent=self.cancelled_assignment(parent,str(i))
            repeated=Service(self.root).dispatch('codex',CODEX,parent)
            self.assertTrue(repeated['reused'])
            self.assertEqual(repeated['status'],'sent_awaiting_ack')
        service=Service(self.root)
        extra=service.message('codex',CODEX,'workbuddy',{'key':'task.acp-acceptance.cancel.over-budget',
            'summary':'must not execute','reply_to':parent})['message_id']
        with self.assertRaisesRegex(ValueError,'最多初次派单'):
            service.dispatch('codex',CODEX,extra)
        self.assertEqual(FakeClient.instances[-1].calls.count('prompt'),3)

    def test_user_continuation_extends_only_same_root_without_resetting_attempts(self):
        self.pair();parent=None
        for i in range(3):parent=self.cancelled_assignment(parent,'authorized-'+str(i))
        service=Service(self.root)
        with service.transaction() as db:
            rows=db.execute("SELECT id FROM events WHERE kind='message' ORDER BY id").fetchall();root=rows[0][0]
            text='User explicitly authorizes this project root continuation'
            service.event(db,'codex',CODEX,'user_authorized_continuation',[],dict(root_event_id=root,project_root=str(self.root),max_attempts=4,user_instruction=text,instruction_hash=digest(text)))
            self.assertEqual(service.authorized_attempt_limit(db,root,CODEX),4)
            self.assertEqual(service.authorized_attempt_limit(db,root+999,CODEX),3)
            self.assertEqual(service.authorized_attempt_limit(db,root,DESKTOP),3)
        fourth=self.cancelled_assignment(parent,'authorized-4')
        self.assertEqual(FakeClient.instances[-1].calls.count('prompt'),4)
        fifth=service.message('codex',CODEX,'workbuddy',{'key':'task.acp-acceptance.cancel.authorized-over','summary':'must stop','reply_to':fourth})['message_id']
        with self.assertRaises(ValueError):service.dispatch('codex',CODEX,fifth)

    def test_shell_commands_really_execute_in_project_from_foreign_host_cwd(self):
        import shlex,shutil,subprocess
        git=shutil.which('git')
        bash=Path(git).parents[1]/'bin/bash.exe' if sys.platform=='win32' and git else Path('/bin/bash')
        if not bash.is_file():self.skipTest('Git Bash unavailable')
        self.pair();service=Service(self.root)
        out=self.root/'.work/acp_acceptance';out.mkdir(parents=True)
        event=service.message('codex',CODEX,'workbuddy',{'key':'task.acp-acceptance.cwd','summary':'verify actual shell cwd'})['message_id']
        command=shlex.join([Path(sys.executable).as_posix(),'-X','utf8','-c',"from pathlib import Path;print(Path.cwd());Path('.work/acp_acceptance/probe.txt').write_text('bound')"])+" > .work/acp_acceptance/cwd.log"
        helper=shlex.join([sys.executable,'-X','utf8',str(self.root/'setup/collaboration/project_sync.py'),'status'])
        self.runtime.set_policy(CODEX,event,['.work/acp_acceptance'],[helper,command],10)
        policy=json.loads((self.runtime.folder/f'task-{event}.json').read_text('utf-8'))
        self.assertEqual(policy['commands'][0],helper)
        self.assertTrue(policy['commands'][1].startswith('cd -- '))
        self.runtime.set_policy(CODEX,event,['.work/acp_acceptance'],policy['commands'],10)
        with tempfile.TemporaryDirectory(dir=BASE) as foreign:
            result=subprocess.run([str(bash),'-s'],input=(policy['commands'][1]+'\n').encode('utf-8'),cwd=foreign,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
        self.assertEqual((out/'probe.txt').read_text(),'bound')
        self.assertEqual(Path((out/'cwd.log').read_text().strip()).resolve(),self.root.resolve())

    def test_unknown_or_unbound_cancel_evidence_never_allows_repair(self):
        self.pair();parent=self.cancelled_assignment()
        service=Service(self.root)
        with service.transaction() as db:
            db.execute("UPDATE desktop_deliveries SET status='unknown_no_resubmit' WHERE event_id=?",(parent,))
        repeated=service.dispatch('codex',CODEX,parent)
        self.assertTrue(repeated['reused'])
        self.assertEqual(repeated['status'],'unknown_no_resubmit')
        self.assertEqual(FakeClient.instances[-1].calls.count('prompt'),1)
        request={'key':'task.acp-acceptance.cancel.denied','summary':'must block','reply_to':parent}
        with self.assertRaises(ValueError):service.message('codex',CODEX,'workbuddy',request)
        with service.transaction() as db:
            db.execute("UPDATE desktop_deliveries SET status='sent_awaiting_ack' WHERE event_id=?",(parent,))
        p=self.runtime.folder/f'result-{parent}.json'
        result=json.loads(p.read_text('utf-8'));result['cwd']=str(self.root.parent)
        atomic_json(p,result)
        with self.assertRaises(ValueError):service.message('codex',CODEX,'workbuddy',request)

    def test_cancel_evidence_rechecked_after_prepare_before_claim(self):
        self.pair();parent=self.cancelled_assignment()
        service=Service(self.root)
        event=service.message('codex',CODEX,'workbuddy',{'key':'task.acp-acceptance.cancel.race',
            'summary':'must block changed evidence','reply_to':parent})['message_id']
        import desktop_bridge
        original=desktop_bridge.prepare
        def prepare(*args,**kwargs):
            prepared=original(*args,**kwargs)
            state=self.runtime.read_state();state['last_event']=999;self.runtime.save_state(state)
            return prepared
        with patch('workbuddy_acp_bridge.project.runtime_for',return_value=self.runtime), \
             patch.object(desktop_bridge,'prepare',side_effect=prepare):
            with self.assertRaisesRegex(ValueError,'取消证据变化'):
                service.dispatch('codex',CODEX,event)
        with service.transaction() as db:
            self.assertIsNone(db.execute('SELECT * FROM desktop_deliveries WHERE event_id=?',(event,)).fetchone())

    def test_transport_failure_finalizes_unknown_and_releases_reservation(self):
        self.pair()
        service=Service(self.root)
        event=service.message('codex',CODEX,'workbuddy',
            {'key':'task.acp-acceptance.dispatch-failure','summary':'fixture'})['message_id']
        self.runtime.set_policy(CODEX,event,['.work/acp_acceptance'],[],2)
        FakeClient.fail_run=True
        with patch('workbuddy_acp_bridge.project.runtime_for',return_value=self.runtime):
            result=service.dispatch('codex',CODEX,event)
        self.assertEqual(result['status'],'unknown_no_resubmit')
        with service.transaction() as db:
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='desktop_dispatch_reservation'").fetchone())

    def test_second_process_lock_denied(self):
        other=ProjectRuntime(self.root,discovery=self.discovery,client_factory=FakeClient)
        try:
            with self.runtime.task_lock():
                with self.assertRaises(ProjectBlocked):
                    with other.task_lock():pass
        finally:other.close()

    def test_unverified_scope_rejects_business_changes(self):
        self.pair()
        service=Service(self.root)
        event=service.message('codex',CODEX,'workbuddy',{'key':'task.acp-acceptance.unit','summary':'test'})['message_id']
        with self.assertRaises(ProjectBlocked):self.runtime.set_policy(CODEX,event,['setup/anything.py'],[],10)
        self.runtime.set_policy(CODEX,event,['.work/acp_acceptance'],[],10)
        with self.assertRaises(ProjectBlocked):self.runtime.set_policy(CODEX,event,['.work/acp_acceptance/else'],[],10)

    def test_permission_paths_commands_and_private_data(self):
        p=PermissionPolicy(self.root,['.work/acp_acceptance'],['approved command'])
        def req(name,**args):return {'toolCall':{'rawInput':args,'_meta':{'codebuddy.ai/toolName':name}}}
        self.assertTrue(p(req('Write',file_path=str(self.root/'.work/acp_acceptance/test.py'))))
        self.assertFalse(p(req('Write',file_path='.work/acp_acceptance/test.py')))
        self.assertFalse(p(req('Write',file_path=str(self.root/'AGENTS.md'))))
        self.assertFalse(p(req('Read',file_path=str(self.root.parent/'unrelated.txt'))))
        self.assertFalse(p(req('Read',file_path=str(self.root/'private/key.txt'))))
        self.assertTrue(p(req('Read',file_path=str(self.root/'AGENTS.md'))))
        self.assertTrue(p(req('Bash',command='approved command')))
        self.assertFalse(p(req('Bash',command='different command')))
        scoped=PermissionPolicy(self.root,['.work/acp_acceptance'],[],HIDDEN)
        request=req('Read',file_path=str(self.root/'AGENTS.md'))
        request['sessionId']=DESKTOP
        self.assertFalse(scoped(request))
        request['sessionId']=HIDDEN
        self.assertTrue(scoped(request))


class DiscoveryTests(unittest.TestCase):
    def test_loopback_normalization(self):
        self.assertEqual(GatewayDiscovery.validate_base_url('http://127.0.0.1:123/api/v1/acp'),'http://127.0.0.1:123')
        for bad in ['http://example.com:123','http://127.0.0.1:123/other','http://a:b@127.0.0.1:123']:
            with self.assertRaises(GatewayDiscoveryError):GatewayDiscovery.validate_base_url(bad)

    def test_only_installed_live_host_is_selected(self):
        with tempfile.TemporaryDirectory(dir=BASE) as t:
            root=Path(t);(root/'sessions').mkdir();exe=root/'WorkBuddy.exe';exe.touch()
            d={'cwd':str(root/'workbuddy-host-cli'/'host'),'pid':22,'lastHeartbeat':time.time()*1000,
               'endpoint':'http://127.0.0.1:123/api/v1/acp'}
            atomic_json(root/'sessions/22.json',d)
            discovery=GatewayDiscovery(root,executable=exe,image_reader=lambda p:exe.resolve())
            self.assertEqual(discovery.discover_host().pid,22)
            d['lastHeartbeat']-=100000;atomic_json(root/'sessions/22.json',d)
            with self.assertRaises(GatewayDiscoveryError):discovery.discover_host()


if __name__=='__main__':unittest.main(verbosity=2)
