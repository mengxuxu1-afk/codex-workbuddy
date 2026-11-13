"""Bound, short-lived access to the user's existing WorkBuddy native runtime.

Only the verified native_reply route remains: read the project's session
registry, confirm the bound live session, POST one reply into that existing
session. The retired ACP connect/initialize/session/prompt transport and the
enterprise OAuth adapters are gone; no fallback to either is possible.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

import httpx

WORKER_BUDGETS = {'preflight': 15, 'submit': 90, 'runtime_status': 15}
WORKER_GRACE = 5
REGISTRY_MAX_AGE_MS = 90000  # Desktop 5.9.2 publishes once per 30 seconds.
FAILURE_MESSAGES = {
    'local_unavailable': '本机WorkBuddy连接未就绪；未发送任务',
    'registry_unavailable': '本机WorkBuddy连接登记目录不可用；未发送任务',
    'session_not_registered': '绑定原会话尚未登记；打开历史窗口不等于运行连接已建立，未发送任务',
    'session_stale': '绑定原会话心跳已过期；未读取口令或发送任务',
    'session_ambiguous': '绑定原会话存在多个连接登记；未读取口令或发送任务',
    'registry_invalid': '绑定原会话的连接地址或进程登记无效；未读取口令或发送任务',
    'wrong_live_session': '本机接口当前活会话与绑定原会话不符；未发送任务',
    'worker_failed': '本机连接检查进程未获得明确结果；不得盲目重发',
}


class LocalBlocked(ValueError):
    def __init__(self, message, *, code='local_unavailable'):
        super().__init__(message)
        self.code = code if code in FAILURE_MESSAGES else 'local_unavailable'


def discover(root, session, directory=None, *, include_pid=True):
    directory = directory or Path.home() / '.workbuddy/sessions'
    directory = Path(directory)
    if (not directory.is_dir() or directory.is_symlink() or directory.is_junction()
            or directory.parent.is_symlink() or directory.parent.is_junction()):
        raise LocalBlocked(FAILURE_MESSAGES['registry_unavailable'], code='registry_unavailable')
    paths = list(directory.glob('*.json'))
    if len(paths) > 256:
        raise LocalBlocked('本机连接登记超过限定数量')
    candidates = []
    matched = 0
    stale = False
    for path in paths:
        try:
            if path.is_symlink() or path.is_junction() or path.stat().st_size > 65536:
                continue
            value = json.loads(path.read_text('utf-8-sig'))
            if not isinstance(value, dict) or value.get('sessionId') != session:
                continue
            if not isinstance(value.get('cwd'), str) or Path(value['cwd']).resolve() != root:
                continue
            matched += 1
            heartbeat = value.get('lastHeartbeat')
            if (isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float))
                    or not 0 <= time.time() * 1000 - heartbeat <= REGISTRY_MAX_AGE_MS):
                stale = True
                continue
            endpoint = value.get('endpoint')
            parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
            if (not parsed or parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
                    or not parsed.port or parsed.username or parsed.password or parsed.query
                    or parsed.fragment or parsed.path not in {'', '/', '/api/v1/acp'}):
                continue
            pid = value.get('pid')
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                continue
            # Registry metadata may point to ACP. This is only an HTTP base;
            # ReplyConnection still permits live/reply and never connects ACP.
            base = parsed._replace(path='', query='', fragment='').geturl()
            candidates.append(dict(endpoint=base, session=session,
                                   root=str(root), pid=pid))
        except (OSError, ValueError, UnicodeError):
            continue
    if matched != 1 or len(candidates) != 1:
        code = ('session_ambiguous' if matched > 1 else 'session_not_registered' if not matched
                else 'session_stale' if stale else 'registry_invalid')
        raise LocalBlocked(FAILURE_MESSAGES[code], code=code)
    return candidates[0]


def read_gateway_password(read_word, byte_length):
    """Inspect one UTF-16 code unit at a time; decode ONLY the allowed value.

    Non-target values are skipped to their delimiter without creating strings,
    blocks, lists, logs or snapshots of those values.
    """
    prefix = tuple(map(ord, 'CODEBUDDY_GATEWAY_PASSWORD='))
    offset = 0
    while offset < byte_length:
        matched = 0
        while offset < byte_length:
            word = read_word(offset)
            offset += 2
            if word == 0:
                break
            if matched < len(prefix) and word == prefix[matched]:
                matched += 1
                if matched == len(prefix):
                    value = bytearray()
                    try:
                        while offset < byte_length and len(value) < 8192:
                            word = read_word(offset)
                            offset += 2
                            if word == 0:
                                if value:
                                    return value.decode('utf-16-le')
                                break
                            value.extend(word.to_bytes(2, 'little'))
                    finally:
                        value[:] = b'\0' * len(value)
                    raise LocalBlocked('绑定进程的临时Gateway口令长度异常')
            else:
                # Skip the remainder of a non-target entry without collecting it.
                while offset < byte_length and word != 0:
                    word = read_word(offset)
                    offset += 2
                break
    raise LocalBlocked('绑定进程中没有可用的临时Gateway口令')


def process_gateway_password(pid, executable):
    """Read-only, bound Windows runtime. No account/profile files or memory scan.

    Desktop 5.9.2 injects this ephemeral credential into its CLI child, instead
    of settings.json. The known PEB environment pointer is the sole read target.
    The caller must check both scoped authorization and live session identity.
    """
    if os.name != 'nt':
        raise LocalBlocked('本机进程鉴权仅支持已核验的Windows桌面版')
    import ctypes as c
    from ctypes import wintypes as w
    if c.sizeof(c.c_void_p) != 8:
        raise LocalBlocked('本机进程布局未经核验')
    kernel = c.WinDLL('kernel32', use_last_error=True)
    native = c.WinDLL('ntdll')
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    native.NtQueryInformationProcess.argtypes = [w.HANDLE, w.ULONG, c.c_void_p, w.ULONG, c.POINTER(w.ULONG)]

    class BasicInfo(c.Structure):
        _fields_ = [('reserved', c.c_void_p), ('peb', c.c_void_p),
                   ('reserved2', c.c_void_p * 2), ('pid', c.c_size_t), ('reserved3', c.c_void_p)]

    expected = Path(executable).resolve(strict=True)
    if expected.name.lower() != 'workbuddy.exe':
        raise LocalBlocked('未绑定WorkBuddy桌面安装程序')
    handle = kernel.OpenProcess(0x410, False, pid)  # QUERY_INFORMATION | VM_READ, never VM_WRITE.
    if not handle:
        raise LocalBlocked('无法只读打开绑定的WorkBuddy进程')
    buffers = []

    def read(address, size):
        if not address or not 0 < size <= 1024 * 1024:
            raise LocalBlocked('绑定进程环境布局异常')
        buffer = c.create_string_buffer(size)
        buffers.append(buffer)
        count = c.c_size_t()
        if not kernel.ReadProcessMemory(handle, address, buffer, size, c.byref(count)) or count.value != size:
            raise LocalBlocked('无法读取绑定进程的环境区域')
        return buffer

    try:
        name = c.create_unicode_buffer(32768)
        size = w.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, c.byref(size)) or Path(name.value).resolve() != expected:
            raise LocalBlocked('绑定进程的安装路径已变化')
        info = BasicInfo()
        if native.NtQueryInformationProcess(handle, 0, c.byref(info), c.sizeof(info), None) != 0:
            raise LocalBlocked('无法定位绑定进程环境')
        parameters = int.from_bytes(read(info.peb + 0x20, 8).raw, 'little')
        address = int.from_bytes(read(parameters + 0x80, 8).raw, 'little')
        length = int.from_bytes(read(parameters + 0x3f0, 8).raw, 'little')
        if length % 2:
            raise LocalBlocked('绑定进程环境编码异常')
        if not 0 < length <= 1024 * 1024:
            raise LocalBlocked('绑定进程环境长度异常')

        def read_word(offset):
            word = c.c_uint16()
            count = c.c_size_t()
            try:
                if not kernel.ReadProcessMemory(handle, address + offset, c.byref(word), 2, c.byref(count)) or count.value != 2:
                    raise LocalBlocked('无法读取绑定进程环境标记')
                return word.value
            finally:
                word.value = 0

        return read_gateway_password(read_word, length)
    finally:
        for buffer in buffers:
            c.memset(buffer, 0, c.sizeof(buffer))
        kernel.CloseHandle(handle)


def bounded_json(response, deadline):
    """Shared 64KiB / deadline bound for the one local reply JSON response."""
    raw = bytearray()
    for chunk in response.iter_bytes():
        raw.extend(chunk)
        if len(raw) > 65536 or time.monotonic() > deadline:
            raise LocalBlocked('本机原会话响应超过64KiB或时间预算')
    return json.loads(raw)


class ReplyConnection:
    """Existing desktop's authenticated live/reply route, no session takeover."""
    def __init__(self, endpoint, *, client=None):
        self.endpoint = endpoint
        self.client = client or httpx.Client(timeout=httpx.Timeout(10, read=20), trust_env=False,
                                             follow_redirects=False, limits=httpx.Limits(max_connections=1))
        self.headers = {}
        self.deadline = time.monotonic() + 90

    def __enter__(self):
        try:
            password = process_gateway_password(self.endpoint['pid'], self.endpoint['executable'])
            self.headers = {'Authorization': 'Bearer ' + password, 'X-CodeBuddy-Request': '1'}
            self.check_bound_runtime()
            return self
        except BaseException:
            self.__exit__()
            raise

    def request(self, method, path, **kwargs):
        allowed = {('GET', '/api/v1/sessions/live'),
                   ('POST', '/api/v1/sessions/' + self.endpoint['session'] + '/reply')}
        if (method, path) not in allowed:
            raise LocalBlocked('不允许读取历史、切换或新建会话')
        with self.client.stream(method, self.endpoint['endpoint'] + path, headers=self.headers, **kwargs) as response:
            if response.status_code != 200:
                raise LocalBlocked('本机原会话接口未明确接受请求')
            value = bounded_json(response, self.deadline)
        value = value.get('data', value)
        if not isinstance(value, dict):
            raise LocalBlocked('本机原会话回执结构异常')
        return value

    def check_bound_runtime(self):
        value = self.request('GET', '/api/v1/sessions/live')
        if value.get('sessionId') != self.endpoint['session']:
            raise LocalBlocked(FAILURE_MESSAGES['wrong_live_session'], code='wrong_live_session')

    def send(self, prompt, accepted=None):
        # Release only the sender reservation before the normal desktop executor
        # can acquire the edit lease. This callback is NOT a delivery receipt.
        if accepted:
            accepted()
        value = self.request('POST', '/api/v1/sessions/' + self.endpoint['session'] + '/reply', json={'text': prompt})
        if value.get('delivered') is not True:
            raise LocalBlocked('本机原会话没有明确投递回执')
        return 'native-reply:' + str(uuid.uuid4())  # Local correlation, not a platform message ID.

    def __exit__(self, *args):
        self.headers.clear()
        self.client.close()


def _check_authorization(config):
    """All gates before any registry, process-memory or network read."""
    if config.get('workbuddy_local_registry_authorized') is not True:
        raise LocalBlocked('尚未授权只读WorkBuddy本机连接登记')
    if config.get('workbuddy_transport') != 'native_reply':
        raise LocalBlocked('仅允许已验证的native_reply原会话路线；未知、缺失或旧transport已阻塞')
    if config.get('workbuddy_process_gateway_authorized') is not True:
        raise LocalBlocked('尚未授权提取绑定进程的临时Gateway口令')


def _preflight(root, config):
    _check_authorization(config)
    endpoint = discover(root, config['sessions']['workbuddy'])
    endpoint.update(transport='native_reply', executable=config['workbuddy_executable'])
    with ReplyConnection(endpoint):
        pass
    return endpoint


def _submit(endpoint, prompt, accepted=None):
    # Rediscover before any actual reply; never use a stale/reassigned port.
    if endpoint.get('transport') != 'native_reply':
        raise LocalBlocked('仅允许native_reply原会话路线；未知或旧transport已阻塞')
    actual = discover(Path(endpoint['root']), endpoint['session'])
    actual.update(transport='native_reply', executable=endpoint['executable'])
    if actual != endpoint:
        raise LocalBlocked('投递前本机Gateway绑定已变化')
    with ReplyConnection(endpoint) as connection:
        return connection.send(prompt, accepted=accepted)


def run_worker(payload, accepted=None):
    """One owned HTTP process, independent hard deadline, no resident daemon."""
    budget = WORKER_BUDGETS.get(payload.get('action'))
    if budget is None:
        raise LocalBlocked('不支持的本机桥接操作')
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if len(raw) > 16000:
        raise LocalBlocked('本机桥接请求超过16KiB')
    process = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--worker'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(budget + WORKER_GRACE, process.kill)
    timer.daemon = True
    timer.start()
    try:
        process.stdin.write(raw)
        process.stdin.close()
        output = None
        seen_accepted = False
        for _ in range(3):
            line = process.stdout.readline(8193)
            if not line:
                break
            if len(line) > 8192:
                raise LocalBlocked('本机限时进程回执过大')
            message = json.loads(line)
            if message == {'accepted': True}:
                if seen_accepted:
                    raise LocalBlocked('本机限时进程重复接受回执')
                seen_accepted = True
                if accepted:
                    accepted()
            elif isinstance(message, dict) and 'result' in message:
                output = message['result']
            elif isinstance(message, dict) and set(message) == {'error_code'}:
                code = message['error_code']
                if not isinstance(code, str) or code not in FAILURE_MESSAGES:
                    code = 'worker_failed'
                raise LocalBlocked(FAILURE_MESSAGES[code], code=code)
            else:
                raise LocalBlocked('本机限时进程回执异常')
        if process.wait(timeout=3) != 0 or output is None:
            raise LocalBlocked('本机限时进程未获明确回执；不重发')
        return output
    except LocalBlocked:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise LocalBlocked(FAILURE_MESSAGES['worker_failed'], code='worker_failed') from None
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def preflight(root, config):
    _check_authorization(config)
    # Authorization flags travel with the payload; the worker must never infer
    # or hard-code them from the transport value.
    return run_worker(dict(action='preflight', root=str(root), session=config['sessions']['workbuddy'],
        transport='native_reply', executable=config.get('workbuddy_executable'),
        registry_authorized=True, process_authorized=True))


def submit(endpoint, prompt, accepted=None):
    return run_worker(dict(action='submit', endpoint=endpoint, prompt=prompt), accepted=accepted)


def _project_runtime_status(root, expected, *, client=None, discovery=None):
    """Short-lived, authenticated GET only. Never return the gateway credential."""
    from desktop_bridge import load_json
    from workbuddy_acp_bridge.gateway import GatewayDiscovery
    config=load_json(root,'.collab/desktop.json')
    state=load_json(root,'.collab/video-edit/session.json')
    if (config.get('root')!=str(root) or config.get('workbuddy_transport')!='project_acp'
            or config.get('workbuddy_local_registry_authorized') is not True
            or config.get('workbuddy_process_gateway_authorized') is not True
            or config.get('sessions',{}).get('workbuddy')!=expected.get('session')
            or config.get('sessions',{}).get('codex')!=expected.get('codex')
            or state.get('project_root')!=str(root) or state.get('session_id')!=expected.get('session')):
        raise LocalBlocked('项目运行状态鉴权或绑定不符')
    discovery=discovery or GatewayDiscovery(executable=Path(config['workbuddy_executable']))
    ep=discovery.discover_host()
    if ep.pid!=expected.get('pid') or ep.base_url!=expected.get('base_url'):
        raise LocalBlocked('项目Host已变化')
    password=process_gateway_password(ep.pid,discovery.executable)
    headers={'Authorization':'Bearer '+password,'X-CodeBuddy-Request':'1'}
    password=None
    client=client or httpx.Client(timeout=5,trust_env=False,follow_redirects=False)
    try:
        with client.stream('GET',ep.base_url+'/api/v1/status',headers=headers,follow_redirects=False) as response:
            if response.status_code!=200:raise LocalBlocked('运行状态接口未明确返回')
            d=bounded_json(response,time.monotonic()+10)
        if not isinstance(d,dict):raise LocalBlocked('运行状态格式异常')
        d=d.get('data',d)
        if (not isinstance(d,dict) or d.get('activeSessionId')!=expected['session']
                or type(d.get('busy')) is not bool or not isinstance(d.get('runStatus'),str)):
            raise LocalBlocked('运行状态所属会话不符')
        return dict(session_id=expected['session'],pid=ep.pid,busy=d['busy'],status=d['runStatus'],
                    last_completed_at=d.get('lastCompletedAt'),checked_at=time.time())
    finally:
        headers.clear();client.close()


def runtime_status(root, config, ep):
    if (config.get('workbuddy_transport')!='project_acp'
            or config.get('workbuddy_local_registry_authorized') is not True
            or config.get('workbuddy_process_gateway_authorized') is not True):
        raise LocalBlocked('未授权项目Host状态检查')
    return run_worker(dict(action='runtime_status',root=str(root),expected=dict(
        session=config['sessions']['workbuddy'],codex=config['sessions']['codex'],pid=ep.pid,base_url=ep.base_url)))


if __name__ == '__main__':
    watchdog = None
    try:
        if sys.argv[1:] != ['--worker']:
            raise LocalBlocked('仅供原协作入口调用')
        raw = sys.stdin.buffer.read(16001)
        if len(raw) > 16000:
            raise LocalBlocked('请求过大')
        data = json.loads(raw)
        budget = WORKER_BUDGETS[data['action']]
        watchdog = threading.Timer(budget, lambda: os._exit(124))
        watchdog.daemon = True
        watchdog.start()
        if data['action'] == 'preflight':
            result = _preflight(Path(data['root']).resolve(strict=True),
                dict(workbuddy_local_registry_authorized=data.get('registry_authorized') is True,
                     sessions=dict(workbuddy=data['session']),
                     workbuddy_transport=data.get('transport'),
                     workbuddy_executable=data.get('executable'),
                     workbuddy_process_gateway_authorized=data.get('process_authorized') is True))
        elif data['action']=='runtime_status':
            result = _project_runtime_status(Path(data['root']).resolve(strict=True),data['expected'])
        else:
            result = _submit(data['endpoint'], data['prompt'],
                accepted=lambda: print(json.dumps({'accepted': True}), flush=True))
        print(json.dumps({'result': result}, ensure_ascii=False), flush=True)
    except LocalBlocked as exc:
        # Only a fixed diagnostic code crosses the process boundary. Never
        # serialize arbitrary exception text, response bodies or credentials.
        print(json.dumps({'error_code': exc.code}), flush=True)
        sys.exit(2)
    except Exception:
        print(json.dumps({'error_code': 'worker_failed'}), flush=True)
        sys.exit(2)  # Never print tokens, response bodies or arbitrary service errors.
    finally:
        if watchdog:
            watchdog.cancel()
