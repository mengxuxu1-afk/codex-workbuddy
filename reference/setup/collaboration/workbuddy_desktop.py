"""Visible WorkBuddy desktop transport consumed by the existing dispatcher.

Only the fixed desktop conversation SDK is callable. No generic JS/RPC endpoint
is exposed, no permission is automatically approved, and prompts are never retried.
"""
from __future__ import annotations

import ctypes
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

from workbuddy_acp_bridge.project import atomic_json, ProjectBlocked, PermissionPolicy
from workbuddy_acp_bridge.gateway import process_image


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ProjectBlocked('CDP redirect rejected')


def bounded_json(url):
    with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=4) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ProjectBlocked('CDP discovery response too large')
    return json.loads(raw)


def port_owner(port):
    if os.name != 'nt':
        raise ProjectBlocked('Desktop transport currently requires Windows')
    from ctypes import wintypes as w
    api = ctypes.WinDLL('iphlpapi')
    size = w.ULONG(0)
    api.GetExtendedTcpTable(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
    if size.value > 4 * 1024 * 1024:
        raise ProjectBlocked('TCP owner table too large')
    data = ctypes.create_string_buffer(size.value)
    if api.GetExtendedTcpTable(data, ctypes.byref(size), False, socket.AF_INET, 5, 0):
        raise ProjectBlocked('Cannot verify CDP listener owner')
    raw = data.raw
    count = int.from_bytes(raw[:4], 'little')
    owners = set()
    for index in range(count):
        offset = 4 + 24 * index
        row = raw[offset:offset + 24]
        if len(row) != 24:
            raise ProjectBlocked('Invalid TCP owner table')
        state, address, local_port, _, _, pid = __import__('struct').unpack('<6I', row)
        if state == 2 and socket.ntohs(local_port & 0xffff) == port:
            if address != int.from_bytes(socket.inet_aton('127.0.0.1'), 'little'):
                raise ProjectBlocked('CDP listener must bind numeric IPv4 loopback only')
            owners.add(pid)
    if len(owners) != 1:
        raise ProjectBlocked('CDP listener missing or ambiguous')
    return owners.pop()


def process_birth(pid):
    """Bind an open connection to an OS process generation, not a reusable PID."""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = w.HANDLE
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ProjectBlocked('Cannot verify desktop process generation')
    try:
        values = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            raise ProjectBlocked('Cannot verify desktop process creation time')
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle(handle)


def desktop_window_visible(pid):
    """Chromium marks fully occluded windows hidden; check the actual OS window."""
    from ctypes import wintypes as w
    api=ctypes.WinDLL('user32')
    found=[]
    callback=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    @callback
    def inspect(hwnd,_):
        owner=w.DWORD()
        api.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        if owner.value==pid and api.IsWindowVisible(hwnd) and not api.IsIconic(hwnd):
            found.append(True)
        return True
    api.EnumWindows(inspect,0)
    return bool(found)


def restore_desktop_window(connection):
    """Restore only the uniquely identified installed main renderer window."""
    from ctypes import wintypes as w
    title=connection._evaluate('document.title')
    if not isinstance(title,str) or not title or len(title)>1000:
        raise ProjectBlocked('Cannot identify the bound desktop window')
    api=ctypes.WinDLL('user32');found=[]
    api.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
    api.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
    api.ShowWindowAsync.argtypes=[w.HWND,ctypes.c_int]
    callback=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    @callback
    def inspect(hwnd,_):
        owner=w.DWORD();api.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        if owner.value==connection.pid:
            text=ctypes.create_unicode_buffer(1024);api.GetWindowTextW(hwnd,text,1024)
            if text.value==title:found.append(hwnd)
        return True
    api.EnumWindows(inspect,0)
    if len(found)!=1:raise ProjectBlocked('Bound main window missing or ambiguous')
    api.ShowWindowAsync(found[0],9)


def target_for(config):
    port = config.get('desktop_cdp_port', 9333)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ProjectBlocked('Invalid project CDP port')
    executable = Path(config.get('workbuddy_executable', ''))
    if not executable.is_absolute() or not executable.is_file():
        raise ProjectBlocked('WorkBuddy installation not bound')
    pid = port_owner(port)
    if process_image(pid) != executable.resolve(strict=True):
        raise ProjectBlocked('CDP listener belongs to another executable')
    expected = config.get('desktop_executable_sha256')
    if not expected or hashlib.sha256(executable.read_bytes()).hexdigest() != expected:
        raise ProjectBlocked('WorkBuddy executable changed; explicit revalidation required')
    items = bounded_json(f'http://127.0.0.1:{port}/json/list')
    if not isinstance(items, list) or len(items) > 32:
        raise ProjectBlocked('Unexpected CDP target list')
    expected_path = (executable.parent / 'resources/app.asar/renderer/index.html').as_posix().lower()
    found = []
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'page':
            continue
        # WorkBuddy target queries contain account bootstrap data. Never retain,
        # print, or log them; inspect only the installed renderer pathname.
        url = urlsplit(item.get('url', ''))
        if url.scheme != 'file' or unquote(url.path).lstrip('/').lower() != expected_path:
            continue
        ws = urlsplit(item.get('webSocketDebuggerUrl', ''))
        target_id = item.get('id', '')
        if (ws.scheme != 'ws' or ws.hostname != '127.0.0.1' or ws.port != port
                or ws.username or ws.password or ws.query or ws.fragment
                or ws.path != '/devtools/page/' + target_id):
            raise ProjectBlocked('CDP websocket identity mismatch')
        found.append((item['webSocketDebuggerUrl'], pid, target_id))
    if len(found) != 1:
        raise ProjectBlocked('Main desktop renderer missing or ambiguous')
    return found[0]


class DesktopConnection:
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.sid = config.get('native_workbuddy_session')
        import uuid
        uuid.UUID(self.sid)
        self.ws = None
        self.sequence = 0

    def __enter__(self):
        from websockets.sync.client import connect
        address, self.pid, self.target = target_for(self.config)
        self.birth = process_birth(self.pid)
        self.ws = connect(address, open_timeout=5, close_timeout=2, max_size=2*1024*1024,
                          proxy=None, ping_interval=20, ping_timeout=20)
        source = (Path(__file__).with_name('desktop_session.js')).read_text('utf8')
        source = source.replace('__ADAPTER_HASH__',json.dumps(hashlib.sha256(source.encode()).hexdigest()))
        source = source.replace('__PROJECT_ROOT__', json.dumps(str(self.root))).replace(
            '__DESKTOP_SESSION__', json.dumps(self.sid))
        try:
            self._evaluate(source)
        except BaseException:
            self.ws.close()
            raise
        return self

    def __exit__(self, *args):
        if self.ws:
            self.ws.close()

    def _evaluate(self, expression, timeout=20):
        self.sequence += 1
        expected = self.sequence
        self.ws.send(json.dumps(dict(id=expected, method='Runtime.evaluate', params=dict(
            expression=expression, returnByValue=True, awaitPromise=True))))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = json.loads(self.ws.recv(timeout=max(.01, deadline-time.monotonic())))
            if value.get('id') == expected:
                result = value.get('result', {})
                if value.get('error') or result.get('exceptionDetails'):
                    raise ProjectBlocked('Desktop SDK operation rejected; no raw response logged')
                return result.get('result', {}).get('value')
        raise ProjectBlocked('Desktop SDK request timed out; never resubmit a prompt')

    def call(self, operation, *args):
        if operation not in {'metadata', 'select', 'start', 'read', 'cancel', 'terminal'}:
            raise ProjectBlocked('Desktop API operation is not allowed')
        value=self._evaluate('globalThis.__videoEditVisibleBridgeV3.' + operation +
                              '(' + ','.join(json.dumps(x,ensure_ascii=False) for x in args) + ')')
        if operation in {'metadata','select'} and isinstance(value,dict):
            value['renderer_visible']=value.get('visible')
            value['visible']=desktop_window_visible(self.pid)
        return value


def ready(metadata, root, session, *, visible=True):
    return (isinstance(metadata, dict) and metadata.get('session_id') == session
            and isinstance(metadata.get('project_root'),str) and Path(metadata['project_root']).is_absolute()
            and Path(metadata.get('project_root', '')).resolve() == root
            and metadata.get('state') == 'idle' and metadata.get('permission_mode') == 'default-sandbox'
            and metadata.get('pending') is False and metadata.get('background') is False
            and metadata.get('children') is False and metadata.get('selected') is True
            and (not visible or metadata.get('visible') is True))


class DesktopRuntime:
    def __init__(self, root, *, connection_factory=DesktopConnection):
        from project_sync import safe
        self.root = root.resolve(strict=True)
        self.folder = safe(self.root,self.root / '.collab/desktop-cdp')
        self.connection_factory = connection_factory

    def config(self):
        from desktop_bridge import load_json
        c = load_json(self.root, '.collab/desktop.json')
        if c.get('root') != str(self.root) or c.get('enabled') is not True:
            raise ProjectBlocked('Desktop project binding mismatch')
        return c

    def status(self):
        try:
            c = self.config()
            with self.connection_factory(self.root,c) as connection:
                m = connection.call('metadata')
                return dict(transport='desktop_cdp',metadata=m,pid=connection.pid,
                            ready=ready(m,self.root,c['native_workbuddy_session']),
                            route_verified=c.get('workbuddy_route_verified') is True,
                            gateway_connected=True,model_called=False)
        except Exception as e:
            return dict(transport='desktop_cdp',ready=False,gateway_connected=False,
                        error_type=type(e).__name__,model_called=False)

    def bind(self, codex):
        from project_sync import Collaboration
        c = self.config()
        if c['sessions'].get('codex') != codex:
            raise ProjectBlocked('Desktop sender mismatch')
        service=Collaboration(self.root)
        with service.transaction() as db:
            if not service.lease(db) or service.lease(db)['owner'] != 'codex:'+codex:
                raise ProjectBlocked('Binding requires this Codex edit lease')
        with self.connection_factory(self.root,c) as connection:
            m=connection.call('select')
            if not ready(m,self.root,c['native_workbuddy_session']):
                raise ProjectBlocked('Select this desktop chat with default permissions and no pending work')
            proof=dict(project_root=str(self.root),session_id=c['native_workbuddy_session'],
                       pid=connection.pid,metadata=m,model_called=False)
        atomic_json(self.folder/'binding-proof.json',proof)
        if c.get('workbuddy_transport') != 'desktop_cdp':
            atomic_json(self.folder/'previous-transport.json',c)
        c['sessions']['workbuddy']=c['native_workbuddy_session']
        c['workbuddy_transport']='desktop_cdp'
        c['workbuddy_route_verified']=False
        atomic_json(self.root/'.collab/desktop.json',c)
        return dict(status='desktop_bound',**proof,route_verified=False)

    def prepare(self, config):
        if config.get('workbuddy_transport') != 'desktop_cdp':
            raise ProjectBlocked('Visible desktop route not bound')
        if (self.folder/'current.json').exists():
            raise ProjectBlocked('Previous desktop request unresolved; never overlap or replay')
        with self.connection_factory(self.root,config) as connection:
            m=connection.call('metadata')
            if (m.get('state')=='idle' and not m.get('pending') and not m.get('background') and
                    not m.get('children') and m.get('permission_mode')=='default-sandbox'):
                if not m.get('selected'):m=connection.call('select')
                if m.get('selected') and not m.get('visible'):
                    restore_desktop_window(connection)
                    m=connection.call('metadata')
            if not ready(m,self.root,config['sessions']['workbuddy']):
                raise ProjectBlocked('Desktop chat busy, hidden, different, or permissions not default')
            return dict(transport='desktop_cdp',root=str(self.root),
                        session=config['sessions']['workbuddy'],pid=connection.pid)

    def set_policy(self,codex,event,paths,commands,timeout):
        from project_sync import Collaboration,digest,snapshot,safe
        c=self.config()
        if c['sessions'].get('codex') != codex or type(event) is not int:
            raise ProjectBlocked('Desktop assignment identity mismatch')
        if (not isinstance(paths,list) or not paths or len(paths)>30 or
                not all(isinstance(p,str) for p in paths) or not isinstance(commands,list) or
                len(commands)>20 or not all(isinstance(x,str) and len(x)<5000 for x in commands) or
                type(timeout) not in {int,float} or not 1<=timeout<=900):
            raise ProjectBlocked('Invalid bounded desktop task policy')
        PermissionPolicy(self.root,paths,commands,c['sessions']['workbuddy'])
        service=Collaboration(self.root)
        import shlex
        anchor='cd -- '+shlex.quote(self.root.as_posix())+' && '
        commands=[x if x.startswith(anchor) or service.helper_command(
            {'command':x},'workbuddy',c['sessions']['workbuddy']) else anchor+x for x in commands]
        with service.transaction() as db:
            row=db.execute('SELECT * FROM events WHERE id=? AND kind="message"',(event,)).fetchone()
            if not row or row['actor']!='codex' or row['session']!=codex:
                raise ProjectBlocked('Desktop task is not from bound planner')
            d=json.loads(row['details'])
            if d.get('source_tree_hash')!=digest(snapshot(self.root)):
                raise ProjectBlocked('Desktop task source changed')
            if not c.get('workbuddy_route_verified') and not d['key'].startswith('task.bridge-probe.'):
                if not d['key'].startswith('task.desktop-acceptance.') or any(
                    not safe(self.root,self.root/p).is_relative_to(self.root/'.work/desktop_acceptance')
                    for p in paths):
                    raise ProjectBlocked('Unverified desktop allows only isolated acceptance writes')
        policy=dict(event_id=event,project_root=str(self.root),session_id=c['sessions']['workbuddy'],
                    paths=paths,commands=commands,timeout=timeout,source_tree_hash=d['source_tree_hash'])
        from desktop_bridge import notification
        sid=c['sessions']['workbuddy']
        prefix=shlex.quote((self.root/'.work/开发环境/Scripts/python.exe').as_posix())+' -X utf8 '+shlex.quote((self.root/'setup/collaboration/project_sync.py').as_posix())
        flags=' --agent workbuddy --session '+sid
        protocol_path=self.folder/f'protocol-{event}.json'
        notice=notification(self.root,sid,event,d['key'].startswith('task.bridge-probe.'),frozen=False,return_mode='same_call')
        notice += ('\n本次实际任务：'+d['summary']+'\n不要研究协议源码或直接查询SQLite。先Read '+str(protocol_path)+
                   '，按里面的精确命令前台执行（run_in_background=false）；sync返回more=true时必须ack本页ack_token后再sync，直到more=false；历史交接只读，不执行旧任务。'
                   '先写好所有finish/result文件，再finish释放锁、message发送关联结果，然后停止。本次结果由原调用自动返回Codex，不执行dispatch或第二次唤醒。任何权限拒绝即停止本轮并回报，不自行发明命令。')
        if len(notice)>4000:raise ProjectBlocked('Desktop notification exceeds receipt bound')
        policy['notification_text']=notice
        policy['return_mode']='same_call'
        note=lambda name:str(self.root/f'.collab/notes/desktop-{event}-{name}.json')
        protocol=dict(notification=notice,assignment=d['summary'],allowed_commands=commands,
            sync_command=prefix+' sync'+flags,ack_command_template=prefix+' ack'+flags+' --token 本页ack_token',
            received_path=note('receive'),received_template={'notification':notice},
            receive_command=prefix+' receive'+flags+f' --event-id {event} --note-file '+shlex.quote(note('receive')),
            begin_command=prefix+' begin'+flags,
            finish_path=note('finish'),finish_template={'summary':'','tests':'','remaining':''},
            finish_command=prefix+' finish'+flags+' --note-file '+shlex.quote(note('finish')),
            result_path=note('result'),result_template=dict(key=f'result.task.{event}',reply_to=event,summary=''),
            result_command=prefix+' message'+flags+' --to codex --note-file '+shlex.quote(note('result')),
            return_mode='same_call',after_result='STOP; the existing pending call returns the correlated result to Codex. Do not dispatch.')
        if len(json.dumps(policy,ensure_ascii=False).encode('utf8')) > 23000:
            raise ProjectBlocked('Desktop task policy exceeds bounded Hook input')
        p=self.folder/f'task-{event}.json'
        if p.exists() and json.loads(p.read_text('utf8'))!=policy:
            raise ProjectBlocked('Desktop task authority is immutable')
        atomic_json(p,policy)
        atomic_json(protocol_path,protocol)

    def submit(self,prompt,event,session,accepted=None):
        try:
            return self._submit(prompt,event,session,accepted)
        except Exception:
            path=self.folder/f'run-{event}.json'
            if path.is_file():
                state=json.loads(path.read_text('utf8'))
                if not state.get('finished_at'):
                    atomic_json(path,{**state,'phase':'unknown_no_resubmit','last_observed_at':time.time()})
            raise ProjectBlocked('Desktop outcome unconfirmed; no resubmission permitted') from None

    def _submit(self,prompt,event,session,accepted=None):
        from project_sync import safe,digest,snapshot
        c=self.config()
        p=safe(self.root,self.folder/f'task-{event}.json')
        policy=json.loads(p.read_text('utf8'))
        if (p.stat().st_size>65536 or policy.get('event_id')!=event or
                policy.get('session_id')!=session or session!=c['sessions']['workbuddy'] or
                policy.get('source_tree_hash')!=digest(snapshot(self.root))):
            raise ProjectBlocked('Desktop task policy mismatch')
        state_path=self.folder/f'run-{event}.json'
        if state_path.exists():
            raise ProjectBlocked('Existing desktop submission never replays')
        deadline=time.monotonic()+policy['timeout']
        with ExitStack() as stack:
            connection=stack.enter_context(self.connection_factory(self.root,c))
            if not ready(connection.call('metadata'),self.root,session):
                raise ProjectBlocked('Desktop became unavailable before submission')
            state=dict(event_id=event,session_id=session,phase='sending',started_at=time.time())
            atomic_json(state_path,state)
            atomic_json(self.folder/'current.json',dict(event_id=event,session_id=session,expires_at=time.time()+policy['timeout']))
            if accepted:accepted()
            # After this point, failure is unknown and must not trigger a resend.
            connection.call('start',event,prompt,f'video-edit-event-{event}')
            reconnects=0
            while time.monotonic()<deadline:
                try:
                    result=connection.call('read',event)
                except Exception:
                    # Reattach for observation only. A renderer reload loses the
                    # in-memory job and deliberately returns missing_no_resubmit.
                    if reconnects >= 2:
                        raise ProjectBlocked('Desktop observation disconnected; never resend') from None
                    reconnects+=1
                    previous=(connection.pid,getattr(connection,'birth',None))
                    connection=stack.enter_context(self.connection_factory(self.root,c))
                    if (connection.pid,getattr(connection,'birth',None)) != previous:
                        raise ProjectBlocked('Desktop restarted during execution; never resend')
                    continue
                atomic_json(state_path,{**state,**result})
                if result.get('finished_at'):
                    if policy.get('return_mode')=='same_call':
                        if not ready(connection.call('metadata'),self.root,session,visible=False):
                            time.sleep(.25)
                            continue
                        self._collect_result(event,session,state,result)
                    # Only a real terminal callback clears task authority. A
                    # missing callback/connection stays unknown, never replayed.
                    (self.folder/'current.json').unlink(missing_ok=True)
                    return f'desktop-cdp:{event}:{session}'
                if result.get('phase') in {'missing_no_resubmit','unknown'}:
                    raise ProjectBlocked('Desktop execution unknown; do not resubmit')
                time.sleep(.75)
            connection.call('cancel',event)
            raise ProjectBlocked('Desktop deadline; no safe turn-specific cancellation available')

    def _collect_result(self,event,session,state,result):
        """Return the actual correlated message over the original pending call."""
        from project_sync import Collaboration
        service=Collaboration(self.root);c=self.config()
        with service.transaction() as db:
            if service.lease(db):
                raise ProjectBlocked('Worker has not released its edit lease')
            task=db.execute("SELECT * FROM events WHERE id=? AND kind='message'",(event,)).fetchone()
            if not task or task['actor']!='codex' or task['session']!=c['sessions']['codex']:
                raise ProjectBlocked('Completed task does not belong to this planner')
            rows=db.execute("SELECT * FROM events WHERE kind='message' AND actor='workbuddy' AND session=? AND json_extract(details,'$.reply_to')=? AND json_extract(details,'$.to')='codex'",(session,event)).fetchall()
            if len(rows)!=1 or rows[0]['time']<state['started_at']:
                raise ProjectBlocked('Native completion lacks one new correlated result')
            report=json.loads(rows[0]['details'])
            if not report.get('key','').startswith('result.'):
                raise ProjectBlocked('Native completion is not an execution report')
            evidence=dict(event_id=event,session_id=session,project_root=str(self.root),
                transport='desktop_cdp',return_mode='same_call',result_event_id=rows[0]['id'],
                reply_to=event,text=report['summary'],native_terminal=result.get('terminal'),
                permissions=result.get('permissions',[]),prompt_visible=result.get('prompt_visible',False),
                independently_accepted=False)
            service.event(db,'codex',c['sessions']['codex'],'desktop_result_returned',[],
                          {k:v for k,v in evidence.items() if k not in {'text','permissions'}})
        atomic_json(self.folder/f'result-{event}.json',evidence)
        return evidence

    def close(self):
        pass  # Connections are short-lived context managers; no watcher/model daemon.

    def cancel(self,codex):
        c=self.config()
        if c['sessions'].get('codex')!=codex:
            raise ProjectBlocked('Only the bound planner may cancel')
        active=json.loads((self.folder/'current.json').read_text('utf8'))
        if active.get('session_id')!=c['sessions']['workbuddy']:
            raise ProjectBlocked('Active desktop binding mismatch')
        with self.connection_factory(self.root,c) as connection:
            return connection.call('cancel',active['event_id'])

    def recover(self,codex):
        c=self.config()
        if c['sessions'].get('codex')!=codex:
            raise ProjectBlocked('Desktop recovery sender mismatch')
        from project_sync import Collaboration
        service=Collaboration(self.root)
        with service.transaction() as db:
            lease=service.lease(db)
            if not lease or lease['owner']!='codex:'+codex:
                raise ProjectBlocked('Terminal reconciliation requires the planner lease')
        current=self.folder/'current.json'
        if current.is_file():
            active=json.loads(current.read_text('utf8'));event=active['event_id']
            run_path=self.folder/f'run-{event}.json';run=json.loads(run_path.read_text('utf8'))
            if active.get('session_id')!=c['sessions']['workbuddy'] or run.get('session_id')!=active['session_id']:
                raise ProjectBlocked('Terminal reconciliation session mismatch')
            start=run['started_at'];start=start*1000 if start<10**12 else start
            with self.connection_factory(self.root,c) as connection:
                metadata=connection.call('metadata')
                terminal=connection.call('terminal',event,start)
                if not ready(metadata,self.root,active['session_id'],visible=False) or not terminal.get('matched'):
                    raise ProjectBlocked('Exact native terminal and idle not confirmed; keep unknown')
                proof=dict(event_id=event,project_root=str(self.root),session_id=active['session_id'],
                           pid=connection.pid,terminal=terminal,unknown_delivery_preserved=True)
            with service.transaction() as db:
                audit=service.event(db,'codex',codex,'desktop_terminal_observed',[],proof)
            atomic_json(run_path,{**run,'terminal_observation':proof,'terminal_audit_event':audit})
            current.unlink()
        return self.status()

    def pair(self,*args,**kwargs):
        raise ProjectBlocked('Visible desktop preserves the existing chat; no create/reset operation')


def task_guard(root,session,name,inputs):
    """A native PreToolUse denial is distinct from automatically granting access."""
    from desktop_bridge import load_json
    from project_sync import safe
    current=root/'.collab/desktop-cdp/current.json'
    if not current.is_file():
        return None
    try:
        c=load_json(root,'.collab/desktop.json')
        if c.get('workbuddy_transport')!='desktop_cdp':
            return None
        if c['sessions']['workbuddy']!=session:
            return '当前任务属于另一个已绑定桌面会话'
        active=load_json(root,'.collab/desktop-cdp/current.json')
        if active.get('session_id')!=session:
            return '桌面任务会话不匹配'
        event=active['event_id']
        policy=load_json(root,f'.collab/desktop-cdp/task-{event}.json')
        if policy.get('event_id')!=event or policy.get('session_id')!=session:
            return '桌面任务策略不匹配'
        args=dict(inputs)
        if args.get('run_in_background'):
            return '桌面协作任务须前台执行；禁止后台命令留下未完成工作'
        if time.time()>active.get('expires_at',float('inf')):
            reading=name.lower() in {'read','grep','glob'}
            target=args.get('file_path',args.get('path',''))
            reporting=isinstance(target,str) and bool(target) and safe(root,Path(target)).is_relative_to(root/'.collab/notes')
            if not reading and not reporting:
                return '本次执行期限已到；仅允许只读诊断和协作失败回报，不继续执行代码任务'
        if name.lower()=='glob':
            pattern=args.get('pattern','')
            if (not isinstance(pattern,str) or Path(pattern).is_absolute() or ':' in pattern
                    or '..' in pattern.replace('\\','/').split('/')):
                return 'Glob范围越界'
        if name.lower()=='glob' and 'path' not in args and 'directory' not in args:
            args['path']=str(root)
        if any(k in args for k in ['file_path','path','directory','filePath']):
            for key in ['file_path','path','directory','filePath']:
                if key in args and isinstance(args[key],str) and not Path(args[key]).is_absolute():
                    args[key]=str(safe(root,root/args[key]))
        params=dict(sessionId=session,toolCall={'_meta':{'codebuddy.ai/toolName':name},'rawInput':args})
        if not PermissionPolicy(root,policy['paths'],policy['commands'],session)(params):
            return '操作不在本次Codex派单范围内；保留桌面原生权限流程，不扩权'
    except (ValueError,KeyError,TypeError,OSError):
        return '桌面任务策略无法核验'
    return None


def trusted_executable(executable):
    """Verify Authenticode with Windows; no shell or execution-policy changes.

    WINTRUST_ACTION_GENERIC_VERIFY_V2 and cached trust only, as documented by
    Microsoft WinVerifyTrust/WINTRUST_DATA. The binding separately pins SHA256.
    """
    from ctypes import wintypes as w
    import uuid
    class FileInfo(ctypes.Structure):
        _fields_=[('size',w.DWORD),('path',w.LPCWSTR),('handle',w.HANDLE),('subject',ctypes.c_void_p)]
    class TrustData(ctypes.Structure):
        _fields_=[('size',w.DWORD),('policy',ctypes.c_void_p),('sip',ctypes.c_void_p),
            ('ui',w.DWORD),('revocation',w.DWORD),('choice',w.DWORD),('file',ctypes.POINTER(FileInfo)),
            ('action',w.DWORD),('state',w.HANDLE),('url',w.LPCWSTR),('flags',w.DWORD),
            ('context',w.DWORD),('settings',ctypes.c_void_p)]
    info=FileInfo();info.size=ctypes.sizeof(info);info.path=str(executable)
    data=TrustData();data.size=ctypes.sizeof(data);data.ui=2;data.choice=1
    data.file=ctypes.pointer(info);data.action=1;data.flags=0x1000|0x2000
    action=ctypes.create_string_buffer(uuid.UUID('00aac56b-cd44-11d0-8cc2-00c04fc295ee').bytes_le)
    verify=ctypes.WinDLL('wintrust').WinVerifyTrust
    verify.argtypes=[w.HWND,ctypes.c_void_p,ctypes.POINTER(TrustData)];verify.restype=w.LONG
    try:
        return verify(w.HWND(-1),action,ctypes.byref(data))==0
    finally:
        data.action=2
        verify(w.HWND(-1),action,ctypes.byref(data))


def installed_processes(executable):
    """Only PID/image metadata; never inspect arguments, history or environment."""
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
            ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
            ('flags',w.DWORD),('name',w.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];kernel.CreateToolhelp32Snapshot.restype=w.HANDLE
    kernel.Process32FirstW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==ctypes.c_void_p(-1).value:raise ProjectBlocked('Cannot inspect installed desktop process')
    found=[];entry=Entry();entry.size=ctypes.sizeof(entry)
    try:
        present=kernel.Process32FirstW(handle,ctypes.byref(entry))
        while present:
            if entry.name.lower()==executable.name.lower():
                try:
                    if Path(process_image(entry.pid)).resolve()==executable.resolve():found.append(entry.pid)
                except Exception:
                    raise ProjectBlocked('Cannot verify an existing WorkBuddy process')
            present=kernel.Process32NextW(handle,ctypes.byref(entry))
    finally:kernel.CloseHandle(handle)
    return found


def launch_desktop(root):
    """Compatibility wrapper: project config -> neutral, client-only launcher.

    The client is started once with no project/chat binding; project dispatch
    (DesktopRuntime/target_for) stays untouched and independently bound.
    Only the three client fields are projected from the existing project
    config, and a client-side refusal is reported in the old exception type.
    """
    import workbuddy_client
    config=DesktopRuntime(root).config()
    client_config=dict(executable=config.get('workbuddy_executable'),
                       sha256=config.get('desktop_executable_sha256'),
                       port=config.get('desktop_cdp_port',9333))
    try:
        return workbuddy_client.ensure_client(client_config)
    except workbuddy_client.ClientBlocked as error:
        raise ProjectBlocked(str(error)) from None


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Launch the neutral WorkBuddy client; legacy config compatibility')
    parser.add_argument('--launch',action='store_true',required=True)
    parser.parse_args()
    try:
        print(json.dumps(launch_desktop(Path(__file__).resolve().parents[2]),ensure_ascii=False))
    except Exception as error:
        print(json.dumps(dict(status='blocked',error_type=type(error).__name__,
            message=str(error) if isinstance(error,ProjectBlocked) else 'Desktop startup failed; no credentials logged'),ensure_ascii=False))
        raise SystemExit(2)
