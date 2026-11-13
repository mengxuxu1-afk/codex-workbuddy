"""Neutral WorkBuddy desktop client launcher (standard library only).

This file is intentionally self-contained: it can be copied alone into a
neutral tools directory and run there. It imports no project module and knows
nothing about projects, sessions, chats, roots or working directories.

ensure_client(config) accepts exactly three client fields:
    executable  absolute path of the installed signed WorkBuddy.exe
    sha256      pinned SHA256 of that executable
    port        numeric loopback remote-debugging port (1024..65535)

Startup is serialized across processes by a Windows named mutex derived from
the executable path. An already connected client is reused with the same PID;
an ordinary running client without a valid renderer endpoint is blocked (never
killed, never restarted). No SDK, no chat selection, no model request, no
URL query strings are read, printed or stored.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler
import uuid

CLIENT_FIELDS = frozenset(('executable', 'sha256', 'port'))
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
READY_BUDGET_SECONDS = 30
MUTEX_WAIT_MS = 30000
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102


class ClientBlocked(RuntimeError):
    """Explicit, safe refusal; never falls back to an unsafe shortcut."""


class EndpointAbsent(ClientBlocked):
    """No listening endpoint yet; distinct from an unsafe occupied endpoint."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ClientBlocked('CDP discovery redirect rejected')


def bounded_json(url):
    """Only /json/list is queried; bounded size, no proxy, no redirect."""
    with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=4) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ClientBlocked('CDP discovery response too large')
    return json.loads(raw)


def port_owner(port):
    """Return the single PID owning a numeric IPv4 loopback listener."""
    if os.name != 'nt':
        raise ClientBlocked('Client transport currently requires Windows')
    from ctypes import wintypes as w
    api = ctypes.WinDLL('iphlpapi')
    size = w.ULONG(0)
    api.GetExtendedTcpTable(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
    if size.value > 4 * 1024 * 1024:
        raise ClientBlocked('TCP owner table too large')
    data = ctypes.create_string_buffer(size.value)
    if api.GetExtendedTcpTable(data, ctypes.byref(size), False, socket.AF_INET, 5, 0):
        raise ClientBlocked('Cannot verify CDP listener owner')
    raw = data.raw
    count = int.from_bytes(raw[:4], 'little')
    owners = set()
    for index in range(count):
        offset = 4 + 24 * index
        row = raw[offset:offset + 24]
        if len(row) != 24:
            raise ClientBlocked('Invalid TCP owner table')
        state, address, local_port, _, _, pid = struct.unpack('<6I', row)
        if state == 2 and socket.ntohs(local_port & 0xffff) == port:
            if address != int.from_bytes(socket.inet_aton('127.0.0.1'), 'little'):
                raise ClientBlocked('CDP listener must bind numeric IPv4 loopback only')
            owners.add(pid)
    if not owners:
        raise EndpointAbsent('CDP listener not running')
    if len(owners) != 1:
        raise ClientBlocked('CDP listener ambiguous')
    return owners.pop()


def process_birth(pid):
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ClientBlocked('Cannot verify client process generation')
    try:
        values = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            raise ClientBlocked('Cannot verify client process generation')
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def process_image(pid):
    """Query only the executable image path; never command line or environment."""
    if os.name != 'nt':
        return None
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR,
                                                 ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        name = ctypes.create_unicode_buffer(32768)
        size = w.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return None
        return Path(name.value).resolve()
    finally:
        kernel.CloseHandle(handle)


def trusted_executable(executable):
    """WinVerifyTrust Authenticode check; separate SHA256 pinning also applies."""
    if os.name != 'nt':
        raise ClientBlocked('Signature verification currently requires Windows')
    from ctypes import wintypes as w

    class FileInfo(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('path', w.LPCWSTR), ('handle', w.HANDLE),
                    ('subject', ctypes.c_void_p)]

    class TrustData(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('policy', ctypes.c_void_p), ('sip', ctypes.c_void_p),
                    ('ui', w.DWORD), ('revocation', w.DWORD), ('choice', w.DWORD),
                    ('file', ctypes.POINTER(FileInfo)), ('action', w.DWORD),
                    ('state', w.HANDLE), ('url', w.LPCWSTR), ('flags', w.DWORD),
                    ('context', w.DWORD), ('settings', ctypes.c_void_p)]

    info = FileInfo()
    info.size = ctypes.sizeof(info)
    info.path = str(executable)
    data = TrustData()
    data.size = ctypes.sizeof(data)
    data.ui = 2
    data.choice = 1
    data.file = ctypes.pointer(info)
    data.action = 1
    data.flags = 0x1000 | 0x2000
    action = ctypes.create_string_buffer(uuid.UUID('00aac56b-cd44-11d0-8cc2-00c04fc295ee').bytes_le)
    verify = ctypes.WinDLL('wintrust').WinVerifyTrust
    verify.argtypes = [w.HWND, ctypes.c_void_p, ctypes.POINTER(TrustData)]
    verify.restype = w.LONG
    try:
        return verify(w.HWND(-1), action, ctypes.byref(data)) == 0
    finally:
        data.action = 2
        verify(w.HWND(-1), action, ctypes.byref(data))


def installed_processes(executable):
    """Only PID/image metadata; never arguments, history or environment."""
    if os.name != 'nt':
        raise ClientBlocked('Process discovery currently requires Windows')
    from ctypes import wintypes as w

    class Entry(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                    ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                    ('name', w.WCHAR * 260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ClientBlocked('Cannot inspect installed desktop process')
    found = []
    entry = Entry()
    entry.size = ctypes.sizeof(entry)
    try:
        ctypes.set_last_error(0)
        present = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not present and ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ClientBlocked('Cannot enumerate existing WorkBuddy processes')
        while present:
            if entry.name.lower() == executable.name.lower():
                try:
                    image = process_image(entry.pid)
                    if image is None:
                        raise ClientBlocked('Cannot verify an existing WorkBuddy process')
                    if image == executable.resolve():
                        found.append(entry.pid)
                except Exception:
                    raise ClientBlocked('Cannot verify an existing WorkBuddy process')
            ctypes.set_last_error(0)
            present = kernel.Process32NextW(handle, ctypes.byref(entry))
            if not present and ctypes.get_last_error() != 18:
                raise ClientBlocked('Cannot finish enumerating WorkBuddy processes')
    finally:
        kernel.CloseHandle(handle)
    return found


class _NamedMutex:
    """Cross-process, per-executable serialization with bounded wait and finally cleanup."""

    def __init__(self, executable):
        key = hashlib.sha256(str(executable).lower().encode('utf-8')).hexdigest()
        self.name = 'Local\\WorkBuddyClientLaunch-' + key[:32]
        self.handle = None
        self.kernel = None

    def __enter__(self):
        if os.name != 'nt':
            return self
        from ctypes import wintypes as w
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
        self.kernel.CreateMutexW.restype = w.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.kernel.WaitForSingleObject.restype = w.DWORD
        self.kernel.ReleaseMutex.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise ClientBlocked('Cannot create client launch mutex')
        try:
            result = self.kernel.WaitForSingleObject(self.handle, MUTEX_WAIT_MS)
            if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                if result == WAIT_TIMEOUT:
                    raise ClientBlocked('Another client launch is still in progress')
                raise ClientBlocked('Client launch mutex wait failed')
        except BaseException:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.handle is not None and self.kernel is not None:
                self.kernel.ReleaseMutex(self.handle)
        finally:
            if self.handle is not None and self.kernel is not None:
                self.kernel.CloseHandle(self.handle)
            self.handle = None
        return False


def _validate(config):
    """Strict whitelist: no project/session/root/cwd or any other field."""
    if not isinstance(config, dict) or set(config) != CLIENT_FIELDS:
        raise ClientBlocked('Client config accepts only executable, sha256 and port')
    raw_path, pinned, port = config['executable'], config['sha256'], config['port']
    if not isinstance(raw_path, str) or not raw_path:
        raise ClientBlocked('Invalid client executable')
    executable = Path(raw_path)
    if (not executable.is_absolute() or not executable.is_file()
            or executable.name.lower() != 'workbuddy.exe'):
        raise ClientBlocked('Bound WorkBuddy executable is missing')
    if not isinstance(pinned, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', pinned):
        raise ClientBlocked('Invalid installation hash')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ClientBlocked('Invalid client debugging port')
    return executable.resolve(strict=True), pinned.lower(), port


def renderer_pid(executable, port):
    """Return the installed renderer's loopback PID, or None when not ready.

    Only the /json/list target list is read; only the installed renderer
    pathname is matched. URL query strings are never read, printed or stored.
    No SDK connection, no chat selection, no model request.
    """
    try:
        pid = port_owner(port)
    except EndpointAbsent:
        return None
    if process_image(pid) != executable.resolve():
        raise ClientBlocked('Client port belongs to a different executable')
    birth = process_birth(pid)
    try:
        items = bounded_json(f'http://127.0.0.1:{port}/json/list')
    except (OSError, ValueError):
        return None
    if not isinstance(items, list) or len(items) > 32:
        return None
    expected = (executable.parent / 'resources/app.asar/renderer/index.html').as_posix().lower()
    found = 0
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'page':
            continue
        if not isinstance(item.get('url'), str):
            continue
        url = urlsplit(item['url'])
        if url.scheme != 'file' or unquote(url.path).lstrip('/').lower() != expected:
            continue
        found += 1
    if found > 1:
        raise ClientBlocked('Main client renderer is ambiguous')
    if found == 0:
        return None
    if (process_birth(pid) != birth or port_owner(port) != pid
            or process_image(pid) != executable.resolve()):
        raise ClientBlocked('Client process changed during discovery')
    return pid


def _spawn(executable, port):
    """Detached GUI process: loopback debugging args only, no console attachment."""
    environment = os.environ.copy()
    environment['WORKBUDDY_REMOTE_DEBUGGING_PORT'] = str(port)
    creationflags = 0
    if os.name == 'nt':
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(executable), f'--remote-debugging-port={port}',
         '--remote-debugging-address=127.0.0.1'],
        cwd=executable.parent,
        env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags, close_fds=True,
    )


def ensure_client(config):
    """Ensure exactly one neutral, signed client with a live loopback renderer."""
    executable, pinned, port = _validate(config)
    digest = hashlib.sha256()
    with executable.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != pinned:
        raise ClientBlocked('WorkBuddy executable changed; revalidate the official installation')
    if not trusted_executable(executable):
        raise ClientBlocked('Official WorkBuddy signature is not valid')
    with _NamedMutex(executable):
        pid = renderer_pid(executable, port)
        if pid is not None:
            return dict(status='already_running', pid=pid)
        if installed_processes(executable):
            raise ClientBlocked('Existing WorkBuddy client has no valid loopback renderer endpoint; '
                                'exit it normally first')
        _spawn(executable, port)
        deadline = time.monotonic() + READY_BUDGET_SECONDS
        while time.monotonic() < deadline:
            pid = renderer_pid(executable, port)
            if pid is not None:
                return dict(status='started', pid=pid)
            time.sleep(.5)
        raise ClientBlocked('WorkBuddy client started but loopback renderer is not ready; '
                            'no model called')


def _message_box(message):
    """GUI failure notice only; never opens a console or a resident service."""
    if os.name != 'nt':
        return
    try:
        ctypes.windll.user32.MessageBoxW(0, str(message)[:1000], 'WorkBuddy client', 0x10)
    except Exception:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='Launch the neutral WorkBuddy desktop client once')
    parser.add_argument('--config', required=True, help='JSON file with only executable/sha256/port')
    parser.add_argument('--gui', action='store_true', help='show a message box on failure')
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        with config_path.open('rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ClientBlocked('Client configuration is too large')
        config = json.loads(raw.decode('utf-8'))
        result = ensure_client(config)
        record = dict(status=result['status'], pid=result['pid'])
        target = config_path.parent / 'last-launch.json'
        temporary = target.with_name('.last-launch-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except (ClientBlocked, OSError, ValueError) as error:
        message = str(error) if isinstance(error, ClientBlocked) else 'WorkBuddy client startup failed'
        if args.gui:
            _message_box(message)
        else:
            print(json.dumps(dict(status='blocked', message=message), ensure_ascii=False))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
