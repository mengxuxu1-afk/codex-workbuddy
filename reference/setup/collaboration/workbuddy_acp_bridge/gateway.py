"""Bounded discovery of the existing Desktop Host; never launches a CLI.

Adapted from jie023/workbuddy-acp-bridge (李杰). No account/chat files read.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

MAX_SESSION_FILE_BYTES = 65536
MAX_HEARTBEAT_AGE_SECONDS = 90


class GatewayDiscoveryError(RuntimeError):
    pass


def process_image(pid: int) -> Path | None:
    """Query only the executable path, not command line or environment."""
    if os.name != 'nt':
        return None
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        name = c.create_unicode_buffer(32768)
        size = w.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, c.byref(size)):
            return None
        return Path(name.value).resolve()
    finally:
        kernel.CloseHandle(handle)


@dataclass(frozen=True, slots=True)
class GatewayEndpoint:
    base_url: str
    pid: int
    last_heartbeat: float
    session_file: Path
    session_id: str | None = None
    cwd: Path | None = None

    @property
    def is_host(self) -> bool:
        return self.cwd is not None and 'workbuddy-host-cli' in self.cwd.parts

    @property
    def acp_url(self) -> str:
        return f'{self.base_url}/api/v1/acp'

    @property
    def connect_url(self) -> str:
        return f'{self.base_url}/api/v1/acp/connect'


class GatewayDiscovery:
    def __init__(self, workbuddy_home: Path | None = None, *, executable: Path | None = None,
                 image_reader=process_image):
        self.workbuddy_home = workbuddy_home or Path.home() / '.workbuddy'
        self.executable = executable or Path('D:/Program Files/WorkBuddy/WorkBuddy.exe')
        self.image_reader = image_reader

    @staticmethod
    def validate_base_url(endpoint: str) -> str:
        if not isinstance(endpoint, str):
            raise GatewayDiscoveryError('Host endpoint missing')
        u = urlsplit(endpoint)
        try:
            valid = (u.scheme == 'http' and u.hostname in {'127.0.0.1', '::1', 'localhost'}
                     and u.port and not (u.username or u.password or u.query or u.fragment)
                     and u.path in {'', '/', '/api/v1/acp'})
        except ValueError:
            valid = False
        if not valid:
            raise GatewayDiscoveryError('Host must use a numeric loopback HTTP endpoint')
        host = '[::1]' if u.hostname == '::1' else '127.0.0.1'
        return f'http://{host}:{u.port}'

    def discover_host(self) -> GatewayEndpoint:
        directory = self.workbuddy_home / 'sessions'
        if (not directory.is_dir() or directory.is_symlink() or directory.is_junction()
                or directory.parent.is_symlink() or directory.parent.is_junction()):
            raise GatewayDiscoveryError('WorkBuddy Host registry unavailable')
        paths = list(directory.glob('*.json'))
        if len(paths) > 256:
            raise GatewayDiscoveryError('Host registry exceeds bounded scan')
        found = []
        for path in paths:
            if path.is_symlink() or path.is_junction():
                continue
            try:
                with path.open('rb') as f:
                    raw = f.read(MAX_SESSION_FILE_BYTES + 1)
                if len(raw) > MAX_SESSION_FILE_BYTES:
                    continue
                d = json.loads(raw.decode('utf-8-sig'))
                # Do not retain or report metadata belonging to project workers.
                cwd = d.get('cwd') if isinstance(d, dict) else None
                if not isinstance(cwd, str) or 'workbuddy-host-cli' not in Path(cwd).parts:
                    continue
                pid, hb = d.get('pid'), d.get('lastHeartbeat')
                if (type(pid) is not int or pid <= 0 or type(hb) not in {int, float}
                        or not 0 <= time.time() * 1000 - hb <= MAX_HEARTBEAT_AGE_SECONDS * 1000):
                    continue
                if self.image_reader(pid) != self.executable.resolve(strict=True):
                    continue
                found.append(GatewayEndpoint(self.validate_base_url(d.get('endpoint')), pid, hb,
                                             path, d.get('sessionId'), Path(cwd)))
            except (OSError, ValueError, UnicodeError):
                continue
        if len(found) != 1:
            raise GatewayDiscoveryError('Host not registered' if not found else 'Ambiguous WorkBuddy Hosts')
        return found[0]

    def discover(self) -> GatewayEndpoint:
        # Cancel and run select the same Host, never the latest project worker.
        return self.discover_host()
