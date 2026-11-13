"""Thin transports for project_sync.dispatch; no watcher, model API or chat scan."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess


class BridgeBlocked(ValueError):
    """No message was submitted; safe to retry after resolving the precondition."""
    def __init__(self, message, *, code='bridge_blocked'):
        super().__init__(message)
        self.code = code


def load_json(root, relative):
    """Bounded read of a project-local JSON config; shared with project_sync."""
    from project_sync import safe
    path = safe(root, root / relative)
    if not path.is_file() or path.stat().st_size > 24000:
        raise BridgeBlocked('缺少本项目桥接配置，或配置过大')
    try:
        value = json.loads(path.read_text('utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('configuration must be object')
        return value
    except (ValueError, OSError):
        raise BridgeBlocked('本项目桥接配置无法解析') from None


def prepare(root, config, target):
    """Resolve every precondition before claiming an irreversible submission.

    Only the verified native_reply route and the official Codex queue remain.
    The retired enterprise OAuth / DPAPI / local_gateway transports are gone;
    any unknown, missing or retired value must block before credentials or
    network access, never silently fall back to a removed interface.
    """
    if target == 'codex':
        # Desktop hosts inherit different PATHs. An explicit, project-local
        # binding must never silently fall back to some other installation.
        configured = 'codex_executable' in config
        executable = config.get('codex_executable') if configured else shutil.which('codex')
        try:
            if not isinstance(executable, str) or not executable or '\x00' in executable:
                raise ValueError('missing executable')
            path = Path(executable)
            if (not path.is_absolute() or path.suffix.lower() not in {'.exe', ''}
                    or not path.is_file() or not os.access(path, os.X_OK)):
                raise ValueError('not a directly executable absolute file')
        except (ValueError, OSError):
            if configured:
                raise BridgeBlocked('本项目codex_executable无效或安装已变化；核实绝对可执行路径后再发送') from None
            raise BridgeBlocked('未找到可直接执行的官方codex命令；请在本项目desktop.json配置codex_executable绝对路径') from None
        return str(path)
    if target == 'workbuddy' and config.get('workbuddy_transport') == 'desktop_cdp':
        from workbuddy_desktop import DesktopRuntime
        try:
            return DesktopRuntime(root).prepare(config)
        except (ValueError, OSError, RuntimeError):
            raise BridgeBlocked('桌面原聊天尚未就绪；检查窗口、默认权限及当前任务状态',
                                code='desktop_not_ready') from None
    if target == 'workbuddy' and config.get('workbuddy_transport') == 'project_acp':
        from workbuddy_acp_bridge.project import runtime_for, ProjectBlocked
        try:
            return runtime_for(root).prepare(config)
        except (ValueError, OSError, RuntimeError) as exc:
            raise BridgeBlocked('video-edit隐藏ACP预检未通过；未提交任务', code='acp_not_ready') from None
    if target != 'workbuddy' or config.get('workbuddy_transport') != 'native_reply':
        raise BridgeBlocked('本机WorkBuddy仅允许native_reply原会话路线；未知或旧transport已阻塞，未读取凭据或发起网络')
    import workbuddy_local
    try:
        return workbuddy_local.preflight(root, config)
    except workbuddy_local.LocalBlocked as exc:
        raise BridgeBlocked(workbuddy_local.FAILURE_MESSAGES[exc.code], code=exc.code) from None
    except Exception:
        raise BridgeBlocked('本机WorkBuddy原会话预检失败；未发送任务') from None


def notification(root, session, event_id, connection_probe=False, *, frozen=True, return_mode=None):
    # The real plan/results are local. No source, task prose or credentials leave
    # the project through this notification. Recipient must verify its session.
    # This text is a receipt contract: receive() compares it verbatim.
    # return_mode is keyword-only and defaults to None, so every existing caller
    # (legacy queue / native_reply submit, receive receipts) keeps byte-identical
    # text. Only a desktop same_call policy opts into the newer probe wording.
    prompt = (f'本项目协作事件 {event_id}，目录 {root}，目标会话 {session}。'
              '先核对当前项目与真实session_id；不匹配则停止，不改其他目录。'
              '读取AGENTS.md及setup/collaboration/README.md，执行sync读取该事件及实际差异后ack。'
              '按已授权的Codex计划→WorkBuddy执行→Codex独立验收分工处理；'
              '写入前begin、交付后finish，完成消息用reply_to关联。通知不扩大用户授权。')
    if connection_probe:
        if return_mode == 'same_call':
            # Desktop same_call protocol: the correlated result already returns
            # through the original MCP call, so there is no second dispatch/wake.
            prompt += ('本次仅为只读接通核验，禁止修改源码或执行会写状态的测试。'
                       f'按protocol用result.task.{event_id}消息报告真实cwd、session_id及本次event_id={event_id}，'
                       'reply_to必须关联本事件；先写好finish/result文件，再finish释放锁、'
                       'message发送关联结果并立即停止，结果沿本次原调用自动返回Codex，'
                       '不dispatch、不二次唤醒、不追加确认消息。')
        else:
            # Legacy probe receipt: keep the original bytes verbatim for the
            # native_reply / official queue submit path and receive receipts.
            prompt += ('本次仅为只读接通核验，禁止修改源码或执行会写状态的测试。'
                       f'通过result.bridge-probe消息报告真实cwd、session_id及本次event_id={event_id}，'
                       'reply_to必须关联本事件；finish释放锁后dispatch该回报，随后停止，不追加确认消息。')
    # New desktop tasks freeze their complete notification at planning time.
    # Old deliveries have no field and keep their original receipt contract.
    policy_path=root/f'.collab/desktop-cdp/task-{event_id}.json'
    if frozen and policy_path.is_file():
        policy=load_json(root,f'.collab/desktop-cdp/task-{event_id}.json')
        frozen=policy.get('notification_text')
        if (isinstance(frozen,str) and len(frozen)<=4000 and
                policy.get('event_id')==event_id and policy.get('session_id')==session):
            return frozen
    return prompt


def submit(root, target, session, event_id, prepared, connection_probe=False, accepted=None):
    prompt = notification(root, session, event_id, connection_probe)
    if target == 'workbuddy' and isinstance(prepared, dict) and prepared.get('transport') == 'desktop_cdp':
        from workbuddy_desktop import DesktopRuntime
        try:
            return DesktopRuntime(root).submit(prompt,event_id,session,accepted=accepted)
        except Exception:
            raise ValueError('桌面任务结果未确认；保留unknown且不重发') from None
    if target == 'workbuddy' and isinstance(prepared, dict) and prepared.get('transport') == 'project_acp':
        from workbuddy_acp_bridge.project import runtime_for
        try:
            return runtime_for(root).submit(prompt, event_id, session, accepted=accepted)
        except Exception:
            # Enter the existing durable unknown finalizer, never leak an ACP
            # response or leave a sending reservation after transport failure.
            raise ValueError('项目ACP提交结果未确认；保留unknown且不重发') from None
    if target == 'workbuddy' and isinstance(prepared, dict):
        import workbuddy_local
        try:
            return workbuddy_local.submit(prepared, prompt, accepted=accepted)
        except Exception:
            raise ValueError('本机WorkBuddy投递未获明确结果；必须核接收端，不重发') from None
    if target == 'codex':
        result = subprocess.run([prepared, 'queue', '--thread', session, '--message', prompt, '-C', str(root)],
                                capture_output=True, encoding='utf-8', errors='replace', timeout=15)
        # A successful exit alone is insufficient (some versions warn but exit 0).
        match = re.fullmatch(r'Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})\.\s*', result.stdout)
        if result.returncode != 0 or result.stderr.strip() or not match or match[2] != session:
            raise ValueError('Codex未返回精确队列回执；投递状态待核实')
        return match[1]
    raise BridgeBlocked('不支持的派发目标或投递形态；未发送，不回退已移除的旧接口')
