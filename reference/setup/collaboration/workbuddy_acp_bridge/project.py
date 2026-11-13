"""video-edit session/state/permissions, consumed by MCP and existing dispatch.

One project-local store, no global session pool and no WorkBuddy CLI spawning.
"""
from __future__ import annotations

import atexit
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import contextlib
import json
import math
from pathlib import Path
import os
import shlex
import sys
import threading
import time
import uuid

from .acp_client import WorkBuddyAcpClient, AcpError
from .gateway import GatewayDiscovery

PROJECT_ID = 'video-edit'


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class ProjectBlocked(ValueError):
    pass


class PermissionPolicy:
    """Exact approved commands or scoped file paths; deny unclassified requests."""
    def __init__(self, root: Path, paths: list[str], commands: list[str], session: str | None = None):
        from project_sync import safe
        self.root = root
        self.paths = [safe(root, root / p) for p in paths]
        self.commands = commands
        self.session = session
        if not self.paths:
            raise ProjectBlocked('A task must have an explicit allowed path scope')

    def __call__(self, params):
        from project_sync import safe
        if self.session and (not isinstance(params, dict) or params.get('sessionId') != self.session):
            return False
        call = params.get('toolCall', {}) if isinstance(params, dict) else {}
        raw = call.get('rawInput', {})
        if not isinstance(raw, dict):
            return False
        meta = call.get('_meta', {})
        name = str(meta.get('codebuddy.ai/toolName', '') if isinstance(meta, dict) else '').lower()
        args = raw
        if not isinstance(args, dict):
            return False
        command = args.get('command', args.get('cmd'))
        if isinstance(command, str):
            # Commands are authored by Codex in the scoped task, never inferred
            # from a model permission request. The normal project hook still runs.
            from project_sync import Collaboration as Service
            helper = self.session and Service(self.root).helper_command(
                {'command':command}, 'workbuddy', self.session)
            return bool(helper) or command in self.commands
        if not any(t in name for t in ['read', 'write', 'edit', 'file', 'patch', 'list', 'glob', 'search', 'grep']):
            return False
        values = [args[k] for k in ['file_path', 'path', 'directory', 'filePath'] if k in args]
        if not values or any(not isinstance(v, str) for v in values):
            return False
        if any(not Path(v).is_absolute() for v in values):
            return False  # Desktop tools otherwise resolve relative to Host temp.
        try:
            targets = [safe(self.root, Path(v)) for v in values]
            if any(any(part.lower() in {'private','secrets','credentials'} or part.lower().startswith('.env')
                       for part in p.relative_to(self.root).parts) for p in targets):
                return False
            # A directory grep could recurse into excluded private descendants.
            # Only the concrete existing file required by this task is enabled.
            if 'grep' in name and any(not p.is_file() for p in targets):
                return False
            reading = any(t in name for t in ['read','list','glob','search','grep']) and not any(
                t in name for t in ['write','edit','patch'])
            return reading or all(any(p.is_relative_to(scope) for scope in self.paths) or
                                  p.is_relative_to(self.root / '.collab/notes') for p in targets)
        except (OSError, ValueError):
            return False


class ProjectRuntime:
    def __init__(self, root: Path, *, discovery=None, client_factory=WorkBuddyAcpClient):
        from project_sync import safe
        self.root = root.resolve(strict=True)
        self.folder = safe(self.root, self.root / '.collab/video-edit')
        self.state_path = safe(self.root, self.folder / 'session.json')
        self.discovery = discovery or GatewayDiscovery()
        self.client_factory = client_factory
        self.client = None
        self.endpoint_identity = None
        self.task_mutex = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name='video-edit-acp-io')
        self.thread.start()
        self.closed = False

    def call(self, coroutine, timeout=35):
        f = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return f.result(timeout)
        except FutureTimeout:
            f.cancel()
            raise ProjectBlocked('ACP hard deadline exceeded; submitted work may be unknown') from None

    def read_state(self):
        from project_sync import safe
        p = safe(self.root, self.state_path)
        if not p.exists():
            return None
        with p.open('rb') as f:
            raw = f.read(65537)
        if len(raw) > 65536:
            raise ProjectBlocked('Session state too large')
        s = json.loads(raw)
        if s.get('project_id') != PROJECT_ID or s.get('project_root') != str(self.root):
            raise ProjectBlocked('Session belongs to a different project')
        if s.get('session_id'):
            uuid.UUID(s['session_id'])
        return s

    def save_state(self, s):
        from project_sync import safe
        atomic_json(safe(self.root, self.state_path), s)

    async def connection(self):
        gateway = self.discovery.discover_host()
        identity = (gateway.base_url, gateway.pid)
        dead = (self.client and self.client._listener_task is not None
                and self.client._listener_task.done())
        if self.client and (identity != self.endpoint_identity or dead):
            await self.client.close()
            self.client = None
        if self.client is None:
            candidate = self.client_factory(gateway, permission_mode='deny')
            try:
                await candidate.connect()
            except BaseException:
                await candidate.close()
                raise
            self.client = candidate
            self.endpoint_identity = identity
        return self.client

    async def _check(self):
        await self.connection()  # Real ACP connect + initialize, not just registry.
        s = self.read_state()
        return dict(project_id=PROJECT_ID, project_root=str(self.root), gateway_connected=True,
                    session_id=s.get('session_id') if s else None,
                    session_status=s.get('status') if s else 'unpaired',
                    route_verified=bool(s and s.get('workbuddy_route_verified')),
                    identity_checked=True, model_called=False)

    def status(self):
        try:
            return self.call(self._check(), timeout=50)
        except (ValueError, OSError, AcpError, RuntimeError) as e:
            reasons = {'Host not registered':'host_not_registered',
                       'Ambiguous WorkBuddy Hosts':'host_ambiguous',
                       'WorkBuddy Host registry unavailable':'host_registry_unavailable'}
            return dict(project_id=PROJECT_ID, project_root=str(self.root), gateway_connected=False,
                        route_verified=False, reason=reasons.get(str(e),type(e).__name__), model_called=False)

    @contextlib.contextmanager
    def task_lock(self):
        from project_sync import safe
        if not self.task_mutex.acquire(blocking=False):
            raise ProjectBlocked('A project ACP task is already in progress')
        handle = None
        locked = False
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            handle = safe(self.root, self.folder / 'task.lock').open('a+b')
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                raise ProjectBlocked('Another process owns the project ACP task lock') from None
            yield
        finally:
            if handle:
                if locked:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            self.task_mutex.release()

    def validate_sender(self, session):
        from desktop_bridge import load_json
        config = load_json(self.root, '.collab/desktop.json')
        if (config.get('root') != str(self.root) or config.get('sessions', {}).get('codex') != session
                or not config.get('enabled')):
            raise ProjectBlocked('Codex sender does not match this project')
        return config

    def pair(self, codex_session, confirm, *, reset=False):
        from project_sync import Collaboration as Service
        config = self.validate_sender(codex_session)
        with self.task_lock():
            old = self.read_state()
            if old and not reset:
                if old.get('status') == 'ready':
                    return dict(status='already_paired', session_id=old['session_id'])
                raise ProjectBlocked('Session creation/recovery is not confirmed; no automatic recreation')
            expected = f'reset:{PROJECT_ID}:{old["session_id"]}' if reset and old else PROJECT_ID
            if confirm != expected:
                raise ProjectBlocked('Explicit project/session confirmation required')
            service = Service(self.root)
            with service.transaction() as db:
                lease = service.lease(db)
                if lease and lease['owner'] != f'codex:{codex_session}':
                    raise ProjectBlocked('Another editor holds this project')
                if old and old.get('active_event'):
                    raise ProjectBlocked('Unknown or active task blocks reset')
            if old:
                atomic_json(self.folder / f'archived-session-{old["session_id"]}.json', old)
            # Discovery/initialize failures precede session/new and must not
            # poison the store as an unknown session creation.
            self.call(self.connection())
            s = dict(project_id=PROJECT_ID, project_root=str(self.root), codex_session=codex_session,
                     session_id=None, status='creating', created_at=time.time(),
                     workbuddy_route_verified=False, active_event=None)
            self.save_state(s)
            try:
                sid = self.call(self._create(), timeout=80)
            except BaseException:
                s['status'] = 'creation_unknown'
                self.save_state(s)
                raise
            s.update(session_id=sid, status='ready')
            self.save_state(s)
            # Preserve original desktop binding/history; only the authorized
            # project transport changes to the new hidden execution session.
            config.setdefault('native_workbuddy_session', config['sessions']['workbuddy'])
            config['sessions']['workbuddy'] = sid
            config['workbuddy_transport'] = 'project_acp'
            config['workbuddy_route_verified'] = False
            config['project_id'] = PROJECT_ID
            atomic_json(self.root / '.collab/desktop.json', config)
            return dict(status='paired', session_id=sid, project_root=str(self.root), model_called=False)

    async def _create(self):
        c = await self.connection()
        r = await c.request('session/new', {'cwd':str(self.root), 'mcpServers':[]}, timeout_seconds=25)
        sid = c._extract_session_id(r)
        uuid.UUID(sid)
        await self._ensure_default(c, sid, r)
        c._loaded_sessions.add(sid)
        return sid

    async def _ensure_default(self, c, sid, response=None):
        # Desktop 5.9.2 set_mode returns {}, not {modeId:...}. Confirm with
        # actual modes/current_mode_update metadata, never a fabricated ACK field.
        mode = response.get('modes', {}).get('currentModeId') if isinstance(response, dict) else None
        if mode:
            c._session_modes[sid] = mode
        if c._session_modes.get(sid) == 'default':
            return
        await c.request('session/set_mode', {'sessionId':sid,'modeId':'default'}, timeout_seconds=15)
        if c._session_modes.get(sid) != 'default':
            actual = await c.request('session/load',{'sessionId':sid,'cwd':str(self.root),'mcpServers':[]},timeout_seconds=25)
            mode = actual.get('modes', {}).get('currentModeId') if isinstance(actual, dict) else None
            if mode:
                c._session_modes[sid] = mode
        if c._session_modes.get(sid) != 'default':
            raise ProjectBlocked('Host did not confirm normal permission mode; no model prompt sent')

    def prepare(self, config):
        s = self.read_state()
        if (not s or s.get('status') != 'ready' or s.get('active_event')
                or s.get('session_id') != config.get('sessions', {}).get('workbuddy')):
            raise ProjectBlocked('Hidden session missing, active, unknown or different from binding')
        result = self.status()
        if not result['gateway_connected']:
            raise ProjectBlocked('WorkBuddy Host is not reachable; no task submitted')
        if s.get('execution_readiness'):
            proof=self.call(self._bound_runtime_status(config,s['session_id']),timeout=45)
            if (proof['busy'] or proof['status']!='idle'
                    or proof['pid']!=s['execution_readiness'].get('pid')):
                raise ProjectBlocked('Runtime busy or changed; recheck without replay before dispatch')
        return dict(transport='project_acp', root=str(self.root), session=s['session_id'])

    async def _bound_runtime_status(self, config, sid):
        """Inspect only the loaded project session; no prompt, takeover or clear."""
        if (config.get('workbuddy_local_registry_authorized') is not True
                or config.get('workbuddy_process_gateway_authorized') is not True):
            raise ProjectBlocked('Local registry/process credential authorization is required')
        from workbuddy_local import runtime_status
        c = await self.connection()
        ep = self.discovery.discover_host()
        if (ep.base_url, ep.pid) != self.endpoint_identity:
            raise ProjectBlocked('Host changed during runtime inspection')
        loaded = await c.request('session/load', {'sessionId':sid,'cwd':str(self.root),'mcpServers':[]}, timeout_seconds=25)
        await self._ensure_default(c, sid, loaded)
        c._loaded_sessions.add(sid)
        proof=await asyncio.to_thread(runtime_status,self.root,config,ep)
        if proof.get('session_id')!=sid or proof.get('pid')!=ep.pid:
            raise ProjectBlocked('Status worker returned a different runtime')
        return {**proof,'mode':'default'}

    def recover(self, codex_session):
        """Resolve active scheduling only after remote idle proof. Never replay."""
        from project_sync import Collaboration as Service, digest
        config = self.validate_sender(codex_session)
        if config.get('workbuddy_transport') != 'project_acp':
            raise ProjectBlocked('Recovery is project ACP only')
        service = Service(self.root)
        with self.task_lock():
            s = self.read_state()
            if not s or s.get('session_id') != config.get('sessions',{}).get('workbuddy'):
                raise ProjectBlocked('Project session binding is missing')
            with service.transaction() as db:
                lease = service.lease(db)
                if not lease or lease['owner'] != f'codex:{codex_session}':
                    raise ProjectBlocked('Recovery requires the bound Codex edit lease')
                active = s.get('active_event')
                if active:
                    row = db.execute('SELECT * FROM events WHERE id=?',(active,)).fetchone()
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='desktop_deliveries'").fetchone()
                    delivery = db.execute('SELECT * FROM desktop_deliveries WHERE event_id=?',(active,)).fetchone() if exists else None
                    if (not row or row['actor'] != 'codex' or row['session'] != codex_session
                            or json.loads(row['details']).get('to') != 'workbuddy'
                            or not delivery or delivery['status'] != 'unknown_no_resubmit'):
                        raise ProjectBlocked('Only this project unknown submission may be reconciled')
            proof = self.call(self._bound_runtime_status(config,s['session_id']),timeout=45)
            if proof['busy'] or proof['status'] != 'idle':
                return dict(status='remote_busy',remote=proof,model_called=False,stopped_confirmed=False)
            if active and (type(proof['last_completed_at']) not in {int,float}
                           or not math.isfinite(proof['last_completed_at'])
                           or proof['last_completed_at'] <= delivery['time']*1000):
                raise ProjectBlocked('Remote completion predates the unknown attempt')
            # Recheck both identities and the lease immediately before persisting.
            if config != self.validate_sender(codex_session) or self.read_state() != s:
                raise ProjectBlocked('Binding/state changed during recovery')
            if self.discovery.discover_host().pid != proof['pid']:
                raise ProjectBlocked('Host changed before recovery commit')
            evidence = dict(project_id=PROJECT_ID, project_root=str(self.root),codex_session=codex_session,
                            session_id=s['session_id'], resolved_active_event=active, remote=proof,
                            outcome='stopped_result_unverified', model_called=False)
            with service.transaction() as db:
                lease = service.lease(db)
                if not lease or lease['owner'] != f'codex:{codex_session}':
                    raise ProjectBlocked('Recovery lease changed')
                evidence_id = service.event(db,'codex',codex_session,'acp_runtime_reconciled',[],evidence)
            atomic_json(self.folder/f'recovery-{evidence_id}.json',evidence)
            s.update(status='ready',active_event=None,
                     execution_readiness=dict(event_id=evidence_id,evidence_hash=digest(evidence),pid=proof['pid']))
            if active:
                s['last_unresolved_event'] = active
            self.save_state(s)
            return dict(status='remote_idle_confirmed',session_id=s['session_id'],evidence_event=evidence_id,
                        stopped_confirmed=True,task_outcome_verified=False,route_verified=False,model_called=False)

    def set_policy(self, codex_session, event_id, paths, commands, timeout):
        from project_sync import Collaboration as Service, safe, digest, snapshot
        config = self.validate_sender(codex_session)
        if config.get('workbuddy_transport') != 'project_acp':
            raise ProjectBlocked('Project ACP is not paired')
        if (type(event_id) is not int or not isinstance(paths, list) or len(paths) > 30
                or not all(isinstance(p, str) for p in paths) or not isinstance(commands, list)
                or len(commands) > 20 or not all(isinstance(c, str) and len(c) < 5000 for c in commands)
                or type(timeout) not in {int, float} or not 1 <= timeout <= 900):
            raise ProjectBlocked('Invalid bounded task policy')
        PermissionPolicy(self.root, paths, commands)
        service = Service(self.root)
        # Desktop Host ignores ACP cwd for Bash subprocesses. Bind every
        # approved non-helper shell command explicitly, including redirections.
        anchor='cd -- '+shlex.quote(self.root.as_posix())+' && '
        commands=[command if command.startswith(anchor) or
                  service.helper_command({'command':command},'workbuddy',config['sessions']['workbuddy'])
                  else anchor+command for command in commands]
        with service.transaction() as db:
            row = db.execute('SELECT * FROM events WHERE id=? AND kind="message"', (event_id,)).fetchone()
            if not row or row['actor'] != 'codex' or row['session'] != codex_session:
                raise ProjectBlocked('Task is not a real assignment from the bound Codex')
            d = json.loads(row['details'])
            if d.get('source_tree_hash') != digest(snapshot(self.root)):
                raise ProjectBlocked('Source changed after task planning')
            if (config.get('workbuddy_route_verified') is not True
                    and not service.acp_execution_ready(config, db)):
                if not d['key'].startswith('task.acp-acceptance.'):
                    raise ProjectBlocked('Unverified ACP permits only scoped acceptance tasks')
                base = self.root / '.work/acp_acceptance'
                acceptance_test = self.root / 'setup/collaboration/test_project_acp_acceptance.py'
                if any(not safe(self.root, self.root / p).is_relative_to(base)
                       and safe(self.root, self.root / p) != acceptance_test for p in paths):
                    raise ProjectBlocked('Acceptance writes must stay in the isolated project test directory')
        p = safe(self.root, self.folder / f'task-{event_id}.json')
        policy = dict(event_id=event_id, project_root=str(self.root), source_tree_hash=d['source_tree_hash'],
                      paths=paths, commands=commands, timeout=timeout)
        if p.exists() and json.loads(p.read_text('utf-8')) != policy:
            raise ProjectBlocked('A task policy cannot be replaced with different authority')
        atomic_json(p, policy)

    def submit(self, prompt, event_id, session, accepted=None):
        from project_sync import safe, Collaboration, snapshot, digest
        with self.task_lock():
            s = self.read_state()
            if not s or s.get('session_id') != session or s.get('active_event'):
                raise ProjectBlocked('Session/task binding changed; no resubmission')
            p = safe(self.root, self.folder / f'task-{event_id}.json')
            if not p.is_file():
                raise ProjectBlocked('No scoped ACP task policy')
            with p.open('rb') as f:
                raw = f.read(65537)
            if len(raw) > 65536:
                raise ProjectBlocked('ACP policy too large')
            policy = json.loads(raw)
            service = Collaboration(self.root)
            with service.transaction() as db:
                row = db.execute('SELECT details FROM events WHERE id=? AND kind="message"',(event_id,)).fetchone()
                if (not row or policy.get('event_id') != event_id or policy.get('project_root') != str(self.root)
                        or policy.get('source_tree_hash') != json.loads(row['details']).get('source_tree_hash')
                        or policy.get('source_tree_hash') != digest(snapshot(self.root))):
                    raise ProjectBlocked('Policy is not bound to this task and current source version')
            s['active_event'] = event_id
            self.save_state(s)  # Persist before possible remote submission.
            started = time.monotonic()
            try:
                if accepted:
                    accepted()
                execution_policy = {**policy, 'assignment': json.loads(row['details']).get('summary','')}
                result = self.call(self._run(prompt, session, execution_policy), timeout=policy['timeout'] + 80)
                atomic_json(self.folder / f'result-{event_id}.json', result.to_dict())
                s['active_event'] = None
                s['last_event'] = event_id
                s['last_completed_at'] = time.time()
                self.save_state(s)
            except BaseException as exc:
                s['status'] = 'task_unknown'
                s['last_error'] = dict(error_type=type(exc).__name__, elapsed_seconds=round(time.monotonic()-started,3),
                    transport=getattr(self.client,'last_transport_error',None),
                    last_event_at=getattr(self.client,'last_event_at',None))
                self.save_state(s)
                raise
            return f'project-acp:{event_id}:{session}'

    async def _run(self, prompt, session, policy):
        c = await self.connection()
        response = None
        if session not in c._loaded_sessions:
            response = await c.request('session/load', {'sessionId':session,'cwd':str(self.root),'mcpServers':[]}, timeout_seconds=25)
            c._loaded_sessions.add(session)
        await self._ensure_default(c, session, response)
        c.permission_mode = 'allow_once'
        c.permission_handler = PermissionPolicy(self.root, policy['paths'], policy['commands'], session)
        helper_prefix = shlex.join([Path(sys.executable).as_posix(), '-X', 'utf8',
                                    (self.root/'setup/collaboration/project_sync.py').as_posix()])
        event=policy['event_id']
        note_base=self.root/'.collab/notes'
        paths={name:(note_base/f'workbuddy-{name}-{event}.json').as_posix()
               for name in ['receive','finish','result']}
        flags=f' --agent workbuddy --session {session}'
        protocol=dict(event_id=event,receipt_path=paths['receive'],receipt={'notification':prompt},
            receive_command=helper_prefix+' receive'+flags+f' --event-id {event} --note-file '+shlex.quote(paths['receive']),
            begin_command=helper_prefix+' begin'+flags,
            finish_path=paths['finish'],finish_template=dict(summary='',tests='',remaining=''),
            finish_command=helper_prefix+' finish'+flags+' --note-file '+shlex.quote(paths['finish']),
            result_path=paths['result'],result_template=dict(key=f'result.task.{event}',reply_to=event,summary=''),
            result_command=helper_prefix+' message'+flags+' --to codex --note-file '+shlex.quote(paths['result']))
        envelope = json.dumps(dict(notification=prompt, transport='project_acp',
            assignment=policy.get('assignment',''),
            protocol=protocol,
            task_policy=dict(allowed_paths=policy['paths'], shell_commands=policy['commands'],
                             collaboration_helper_prefix=helper_prefix, agent='workbuddy', session=session),
            instruction='Read the notification field verbatim. Use the project sync/ack/receive/begin/finish protocol. '
                        'The assignment and exact task_policy are provided before any tool call. '
                        'There is no general shell permission: do not run pwd/list/echo startup probes. '
                        'If shell_commands is nonempty, the first shell call must exactly match shell_commands[0]. '
                        'Use absolute project paths with Read/Write/Edit; Host tool cwd is not ACP cwd. '
                        'All non-helper shell_commands include the fixed project cd prefix; copy the complete command. '
                        'Use the exact helper prefix for collaboration commands. '
                        'protocol supplies complete received-notification JSON, note paths, and exact commands: '
                        'Write the received protocol.receipt yourself, then receive/begin; do not research or reinvent these commands. '
                        'Do not prepend PowerShell & in Bash or combine commands. A permission denial stops the turn. '
                        'Return a result.* message with reply_to. This MCP call returns the execution result; '
                        'do not dispatch a second wake notification to Codex. No task outside the actual plan is authorized.'), ensure_ascii=False)
        try:
            async with asyncio.timeout(policy['timeout'] + 40):
                return await c.run(envelope, cwd=self.root, session_id=session,
                                   timeout_seconds=policy['timeout'])
        finally:
            c.permission_handler = None
            c.permission_mode = 'deny'

    def cancel(self, codex_session):
        config=self.validate_sender(codex_session)
        s = self.read_state()
        if not s or not s.get('active_event'):
            return dict(status='no_active_task', stopped_confirmed=False)
        if s.get('session_id')!=config.get('sessions',{}).get('workbuddy'):
            raise ProjectBlocked('Cancel target is not the bound project session')
        async def cancel_bound():
            c=await self.connection()
            loaded=await c.request('session/load',{'sessionId':s['session_id'],'cwd':str(self.root),'mcpServers':[]},timeout_seconds=25)
            await self._ensure_default(c,s['session_id'],loaded)
            await asyncio.wait_for(c.cancel(s['session_id']),timeout=5)
        self.call(cancel_bound(), timeout=40)
        return dict(status='cancel_requested', event_id=s['active_event'], stopped_confirmed=False)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.client:
            with contextlib.suppress(Exception):
                self.call(self.client.close(), timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=6)
        if not self.thread.is_alive():
            self.loop.close()


_runtimes = {}
_runtime_lock = threading.Lock()


def runtime_for(root):
    root = Path(root).resolve(strict=True)
    with _runtime_lock:
        if root not in _runtimes:
            _runtimes[root] = ProjectRuntime(root)
            atexit.register(_runtimes[root].close)
        return _runtimes[root]
