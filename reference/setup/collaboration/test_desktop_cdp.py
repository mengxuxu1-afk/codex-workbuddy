"""Desktop transport behavioral tests; fixture SDK is not real route acceptance."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from workbuddy_desktop import DesktopRuntime, ready, task_guard
from workbuddy_acp_bridge.project import atomic_json, ProjectBlocked
from project_sync import snapshot,digest

SID='00000000-0000-4000-8000-000000000001'
ROOT=Path(__file__).resolve().parents[2]

class DesktopTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/'.work/collaboration_20261111/desktop_tests';folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder);self.root=Path(self.temp.name).resolve()
        self.config=dict(root=str(self.root),enabled=True,workbuddy_transport='desktop_cdp',native_workbuddy_session=SID,
                         sessions={'codex':'c1','workbuddy':SID},workbuddy_route_verified=False)
        atomic_json(self.root/'.collab/desktop.json',self.config)
        self.metadata=dict(session_id=SID,project_root=str(self.root),state='idle',permission_mode='default-sandbox',
                           selected=True,visible=True,pending=False,background=False,children=False)
    def tearDown(self):self.temp.cleanup()
    def connection(self,fail=False):
        owner=self
        class Connection:
            pid=123;birth=456
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,method,*args):
                owner.calls.append(method)
                if method=='metadata':return owner.metadata
                if method=='start' and fail:raise RuntimeError('opaque websocket error')
                if method=='read':return dict(finished_at=1,phase='returned',terminal={'request_id':'new'})
                return {}
        return lambda *args:Connection()
    def policy(self):
        folder=self.root/'.collab/desktop-cdp'
        atomic_json(folder/'task-7.json',dict(event_id=7,session_id=SID,source_tree_hash=digest(snapshot(self.root)),
                    timeout=1,paths=['.work/desktop_acceptance'],commands=[]))
    def test_ready_requires_exact_project_chat_and_ui_preference(self):
        self.assertTrue(ready(self.metadata,self.root,SID))
        for key,value in [('session_id','other'),('project_root',str(self.root.parent)),('permission_mode','fullAccess'),
                          ('project_root',''),('pending',True),('state','running'),('visible',False),('selected',False)]:
            self.assertFalse(ready({**self.metadata,key:value},self.root,SID),key)
    def test_real_terminal_clears_authority_but_not_receipt(self):
        self.calls=[];self.policy();runtime=DesktopRuntime(self.root,connection_factory=self.connection())
        runtime.submit('test',7,SID)
        self.assertFalse((runtime.folder/'current.json').exists())
        self.assertEqual(json.loads((runtime.folder/'run-7.json').read_text())['phase'],'returned')
        with self.assertRaises(ProjectBlocked):runtime.submit('test',7,SID)
        self.assertEqual(self.calls.count('start'),1)
    def test_unknown_preserves_authority_and_never_replays(self):
        self.calls=[];self.policy();runtime=DesktopRuntime(self.root,connection_factory=self.connection(True))
        with self.assertRaises(ProjectBlocked):runtime.submit('test',7,SID)
        self.assertTrue((runtime.folder/'current.json').exists())
        self.assertEqual(json.loads((runtime.folder/'run-7.json').read_text())['phase'],'unknown_no_resubmit')
        with self.assertRaises(ProjectBlocked):runtime.prepare(self.config)
        with self.assertRaises(ProjectBlocked):runtime.submit('test',7,SID)
        self.assertEqual(self.calls.count('start'),1)
    def test_scope_denies_escape_and_other_chat(self):
        self.policy();atomic_json(self.root/'.collab/desktop-cdp/current.json',dict(event_id=7,session_id=SID))
        self.assertIsNotNone(task_guard(self.root,SID,'Glob',{'pattern':'../**/*'}))
        self.assertIsNotNone(task_guard(self.root,'other','Read',{'file_path':str(self.root/'README.md')}))
        self.assertIsNotNone(task_guard(self.root,SID,'Write',{'file_path':str(self.root/'AGENTS.md')}))
        self.assertIsNone(task_guard(self.root,SID,'Write',{'file_path':str(self.root/'.work/desktop_acceptance/demo.py')}))
        active=self.root/'.collab/desktop-cdp/current.json'
        atomic_json(active,dict(event_id=7,session_id=SID,expires_at=time.time()-1))
        self.assertIsNotNone(task_guard(self.root,SID,'Write',{'file_path':str(self.root/'.work/desktop_acceptance/demo.py')}))
        self.assertIsNone(task_guard(self.root,SID,'Write',{'file_path':str(self.root/'.collab/notes/failure.json')}))
    def test_status_does_not_restore_or_confuse_busy_with_offline(self):
        from project_sync import Collaboration
        service=Collaboration(self.root);service.init()
        with patch('workbuddy_desktop.DesktopRuntime.status',return_value=dict(ready=False,gateway_connected=True,metadata={'state':'working'})):
            result=service.connection_check('codex','c1','workbuddy')
        self.assertEqual(result['status'],'desktop_not_ready_at_check')
        self.assertEqual(result['metadata']['state'],'working')
        self.assertFalse(result['model_called'])
    def test_background_commands_including_helper_are_rejected(self):
        from project_sync import Collaboration
        self.policy()
        atomic_json(self.root/'.collab/desktop-cdp/current.json',dict(event_id=7,session_id=SID))
        self.assertIsNotNone(task_guard(self.root,SID,'Bash',dict(command='fixture',run_in_background=True)))
        service=Collaboration(self.root);service.init()
        with patch.object(service,'helper_command',return_value=True):
            outcome=service.hook('workbuddy',dict(cwd=str(self.root),hook_event_name='PreToolUse',session_id=SID,tool_name='Bash',
                tool_input=dict(command='fixture helper',run_in_background=True)))
        self.assertEqual(outcome['hookSpecificOutput']['permissionDecision'],'deny')
    def test_sdk_behavior(self):
        node=shutil.which('node');self.assertIsNotNone(node,'Node runtime required for adapter behavioral tests')
        result=subprocess.run([node,str(Path(__file__).with_name('test_desktop_session.js'))],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_renderer_and_launcher_are_part_of_actual_source_hash(self):
        before=digest(snapshot(self.root))
        for relative in ['setup/collaboration/desktop_session.js','.workbuddy/start-visible.cmd']:
            p=self.root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture','utf8')
            self.assertIn(relative,snapshot(self.root))
        self.assertNotEqual(before,digest(snapshot(self.root)))
    def test_active_desktop_backlog_is_bounded_and_explicitly_acknowledged(self):
        from project_sync import Collaboration
        service=Collaboration(self.root);service.init()
        with service.transaction() as db:
            for n in range(40):service.event(db,'codex','c1','handoff',[],{'summary':str(n)})
        atomic_json(self.root/'.collab/desktop-cdp/current.json',dict(event_id=7,session_id=SID))
        first=service.sync('workbuddy',SID)
        self.assertEqual(len(first['events']),32);self.assertTrue(first['more'])
        self.assertEqual(service.sync('workbuddy',SID)['through_id'],first['through_id'])
        service.ack('workbuddy',SID,first['ack_token'])
        second=service.sync('workbuddy',SID)
        self.assertFalse(second['more']);self.assertTrue(all(x['id']>first['through_id'] for x in second['events']))
    def test_frozen_native_notification_and_exact_protocol_are_idempotent(self):
        from project_sync import Collaboration
        from desktop_bridge import notification
        service=Collaboration(self.root);service.init()
        event=service.message('codex','c1','workbuddy',{'key':'task.desktop-acceptance.unit','summary':'Only a fixture.'})['message_id']
        runtime=DesktopRuntime(self.root)
        runtime.set_policy('c1',event,['.work/desktop_acceptance'],[],3)
        first=notification(self.root,SID,event)
        runtime.set_policy('c1',event,['.work/desktop_acceptance'],[],3)
        self.assertEqual(first,notification(self.root,SID,event))
        packet=json.loads((runtime.folder/f'protocol-{event}.json').read_text('utf8'))
        self.assertEqual(packet['received_template']['notification'],first)
        self.assertIn('ack_token',packet['ack_command_template'])
        self.assertIn(self.root.as_posix(),packet['sync_command'])
        self.assertIn('Only a fixture.',first)
        self.assertEqual(packet['return_mode'],'same_call')
        self.assertNotIn('dispatch_template',packet)
    def test_probe_notification_keeps_legacy_bytes_by_default(self):
        import inspect
        from desktop_bridge import notification
        eid=318
        signature=inspect.signature(notification)
        self.assertEqual(signature.parameters['return_mode'].kind,inspect.Parameter.KEYWORD_ONLY)
        self.assertIsNone(signature.parameters['return_mode'].default)
        base=(f'本项目协作事件 {eid}，目录 {self.root}，目标会话 {SID}。'
              '先核对当前项目与真实session_id；不匹配则停止，不改其他目录。'
              '读取AGENTS.md及setup/collaboration/README.md，执行sync读取该事件及实际差异后ack。'
              '按已授权的Codex计划→WorkBuddy执行→Codex独立验收分工处理；'
              '写入前begin、交付后finish，完成消息用reply_to关联。通知不扩大用户授权。')
        legacy=('本次仅为只读接通核验，禁止修改源码或执行会写状态的测试。'
                f'通过result.bridge-probe消息报告真实cwd、session_id及本次event_id={eid}，'
                'reply_to必须关联本事件；finish释放锁后dispatch该回报，随后停止，不追加确认消息。')
        self.assertEqual(notification(self.root,SID,eid,True,frozen=False),base+legacy)
        self.assertEqual(notification(self.root,SID,eid,True,frozen=False,return_mode=None),base+legacy)
        # Non-probe notices are byte-identical regardless of return_mode.
        self.assertEqual(notification(self.root,SID,eid,frozen=False,return_mode='same_call'),base)
    def test_same_call_probe_notification_has_no_legacy_dispatch_requirement(self):
        from desktop_bridge import notification
        eid=318
        text=notification(self.root,SID,eid,True,frozen=False,return_mode='same_call')
        self.assertIn('只读接通核验',text)
        self.assertIn(f'result.task.{eid}',text)
        self.assertIn('message发送关联结果',text)
        self.assertIn('立即停止',text)
        self.assertIn('不dispatch',text)
        self.assertIn('不二次唤醒',text)
        self.assertNotIn('result.bridge-probe',text)
        self.assertNotIn('dispatch该回报',text)
        self.assertNotIn('finish释放锁后dispatch',text)
        self.assertNotEqual(text,notification(self.root,SID,eid,True,frozen=False))
    def test_set_policy_probe_freezes_protocol_aligned_same_call_notice(self):
        from project_sync import Collaboration
        from desktop_bridge import notification
        service=Collaboration(self.root);service.init()
        event=service.message('codex','c1','workbuddy',
            {'key':'task.bridge-probe.samecall.20261112','summary':'read-only same-call probe fixture'})['message_id']
        runtime=DesktopRuntime(self.root)
        runtime.set_policy('c1',event,['.work/desktop_acceptance'],[],3)
        packet=json.loads((runtime.folder/f'protocol-{event}.json').read_text('utf8'))
        notice=packet['notification']
        # Result key and stop wording must match the frozen protocol.
        self.assertEqual(packet['result_template']['key'],f'result.task.{event}')
        self.assertIn(packet['result_template']['key'],notice)
        self.assertEqual(packet['return_mode'],'same_call')
        self.assertIn('STOP',packet['after_result'])
        self.assertIn('Do not dispatch',packet['after_result'])
        self.assertIn('message发送关联结果',notice)
        self.assertIn('立即停止',notice)
        self.assertIn('不dispatch',notice)
        # The legacy dispatch requirement is gone from the frozen notice.
        self.assertNotIn('dispatch该回报',notice)
        self.assertNotIn('result.bridge-probe',notice)
        self.assertNotIn('finish释放锁后dispatch',notice)
        self.assertNotIn('dispatch_template',packet)
        # Frozen notification keeps priority and set_policy stays idempotent.
        self.assertEqual(notification(self.root,SID,event),notice)
        runtime.set_policy('c1',event,['.work/desktop_acceptance'],[],3)
        self.assertEqual(notification(self.root,SID,event),notice)
        self.assertEqual(json.loads((runtime.folder/f'protocol-{event}.json').read_text('utf8'))['notification'],notice)
    def test_original_call_returns_exact_worker_result_with_independent_acceptance_false(self):
        from project_sync import Collaboration
        service=Collaboration(self.root);service.init()
        event=service.message('codex','c1','workbuddy',{'key':'task.desktop-acceptance.unit','summary':'fixture'})['message_id']
        started=time.time()-1
        service.message('workbuddy',SID,'codex',{'key':'result.unit','reply_to':event,'summary':'actual fixture result'})
        runtime=DesktopRuntime(self.root)
        evidence=runtime._collect_result(event,SID,{'started_at':started},{'terminal':{'state':'completed','request_id':'native-fixture'}})
        self.assertEqual(evidence['text'],'actual fixture result')
        self.assertEqual(evidence['reply_to'],event)
        self.assertFalse(evidence['independently_accepted'])
        self.assertEqual(json.loads((runtime.folder/f'result-{event}.json').read_text('utf8'))['return_mode'],'same_call')
        with service.transaction() as db:db.execute('INSERT OR REPLACE INTO lease VALUES(1,?,?,?,?)',('workbuddy:'+SID,time.time(),time.time(),'{}'))
        with self.assertRaises(ProjectBlocked):runtime._collect_result(event,SID,{'started_at':started},{})
    def test_same_call_rejects_missing_or_ambiguous_worker_report(self):
        from project_sync import Collaboration
        service=Collaboration(self.root);service.init()
        event=service.message('codex','c1','workbuddy',{'key':'task.desktop-acceptance.unit','summary':'fixture'})['message_id']
        runtime=DesktopRuntime(self.root);started=time.time()-1
        with self.assertRaises(ProjectBlocked):runtime._collect_result(event,SID,{'started_at':started},{})
        for i in range(2):service.message('workbuddy',SID,'codex',{'key':f'result.unit{i}','reply_to':event,'summary':'fixture'})
        with self.assertRaises(ProjectBlocked):runtime._collect_result(event,SID,{'started_at':started},{})
    def test_same_call_result_never_queues_a_duplicate_wake(self):
        from project_sync import Collaboration
        service=Collaboration(self.root);service.init()
        event=service.message('codex','c1','workbuddy',{'key':'task.desktop-acceptance.unit','summary':'fixture'})['message_id']
        atomic_json(self.root/f'.collab/desktop-cdp/task-{event}.json',dict(event_id=event,session_id=SID,return_mode='same_call'))
        report=service.message('workbuddy',SID,'codex',{'key':'result.unit','reply_to':event,'summary':'fixture'})['message_id']
        with patch('desktop_bridge.prepare') as prepare:
            response=service.dispatch('workbuddy',SID,report)
        self.assertEqual(response['status'],'returned_via_original_call')
        self.assertFalse(response['queued']);prepare.assert_not_called()
    def test_dispatch_boundary_normalizes_non_os_websocket_errors(self):
        from desktop_bridge import submit
        with patch('workbuddy_desktop.DesktopRuntime.submit',side_effect=RuntimeError('opaque response')):
            with self.assertRaisesRegex(ValueError,'unknown'):
                submit(self.root,'workbuddy',SID,7,{'transport':'desktop_cdp'})

if __name__=='__main__':unittest.main()
