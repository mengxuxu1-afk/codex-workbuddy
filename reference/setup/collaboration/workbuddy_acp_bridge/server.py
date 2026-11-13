"""Project-only MCP facade over the existing collaboration dispatcher.

Adapted from jie023/workbuddy-acp-bridge (李杰). No global ACP session store.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ListToolsResult, CallToolResult

from .project import PROJECT_ID, ProjectBlocked, runtime_for

ROOT = Path(__file__).resolve().parents[3]


def binding():
    root = Path(os.environ.get('WB_PROJECT_ROOT', str(ROOT))).resolve(strict=True)
    if root != ROOT or os.environ.get('WB_PROJECT_ID', PROJECT_ID) != PROJECT_ID:
        raise ProjectBlocked('MCP identity cannot be overridden to another project')
    session = os.environ.get('WB_CODEX_SESSION', '')
    from desktop_bridge import load_json
    config = load_json(root, '.collab/desktop.json')
    if not session:
        session = config.get('sessions', {}).get('codex', '')
    if config.get('workbuddy_transport') == 'desktop_cdp':
        from workbuddy_desktop import DesktopRuntime
        if config.get('root') != str(root) or config.get('sessions',{}).get('codex') != session:
            raise ProjectBlocked('Visible desktop sender binding mismatch')
        runtime = DesktopRuntime(root)
    else:
        runtime = runtime_for(root)
        runtime.validate_sender(session)
    return runtime, session


async def list_tools():
    return [
        Tool(name='workbuddy_status', description='Inspect the explicitly bound local desktop/ACP route; video-edit only. No model call.',
             inputSchema={'type':'object','properties':{},'additionalProperties':False}),
        Tool(name='workbuddy_pair', description='Legacy ACP pairing only; visible desktop route rejects creation/reset and preserves the original chat.',
             inputSchema={'type':'object','properties':{'confirm':{'type':'string'}},'required':['confirm'],'additionalProperties':False}),
        Tool(name='workbuddy_run', description='Dispatch an existing Codex task event after sync/ack/finish. Scoped permissions; no clear.',
             inputSchema={'type':'object','properties':{'event_id':{'type':'integer'},
                'allowed_paths':{'type':'array','items':{'type':'string'},'maxItems':30},
                'allowed_commands':{'type':'array','items':{'type':'string'},'maxItems':20},
                'timeout_seconds':{'type':'number','minimum':1,'maximum':900}},
                'required':['event_id','allowed_paths','allowed_commands'],'additionalProperties':False}),
        Tool(name='workbuddy_cancel', description='Request cancellation of this project active event; no unproven stopped claim.',
             inputSchema={'type':'object','properties':{},'additionalProperties':False}),
        Tool(name='workbuddy_recover', description='Inspect bound remote runtime, reconcile confirmed idle without replay; requires Codex lease. No model call.',
             inputSchema={'type':'object','properties':{},'additionalProperties':False}),
        Tool(name='workbuddy_reset', description='Explicit reset: archive local binding, create a new project session. Never delete Desktop history.',
             inputSchema={'type':'object','properties':{'confirm':{'type':'string'}},'required':['confirm'],'additionalProperties':False}),
    ]


def execute(name, arguments):
    runtime, codex = binding()
    if not isinstance(arguments, dict):
        raise ProjectBlocked('Arguments must be an object')
    allowed = {'workbuddy_status':set(), 'workbuddy_cancel':set(), 'workbuddy_recover':set(),
               'workbuddy_pair':{'confirm'}, 'workbuddy_reset':{'confirm'},
               'workbuddy_run':{'event_id','allowed_paths','allowed_commands','timeout_seconds'}}
    if name not in allowed or set(arguments) - allowed[name]:
        raise ProjectBlocked('Tool arguments cannot override project/session/cwd binding')
    if name == 'workbuddy_status':
        return runtime.status()
    if name in {'workbuddy_pair','workbuddy_reset'}:
        return runtime.pair(codex, arguments.get('confirm'), reset=name=='workbuddy_reset')
    if name == 'workbuddy_cancel':
        return runtime.cancel(codex)
    if name == 'workbuddy_recover':
        return runtime.recover(codex)
    if name != 'workbuddy_run':
        raise ProjectBlocked('Unknown project tool')
    event = arguments.get('event_id')
    runtime.set_policy(codex, event, arguments.get('allowed_paths'), arguments.get('allowed_commands'),
                       arguments.get('timeout_seconds', 300))
    from project_sync import Collaboration as Service
    receipt = Service(runtime.root).dispatch('codex', codex, event)
    result_path = runtime.folder / f'result-{event}.json'
    if result_path.is_file():
        # Keep the MCP context bounded. Complete evidence remains project-local.
        result = json.loads(result_path.read_text('utf-8'))
        receipt['execution'] = {**result, 'text': result.get('text','')[:14000]}
        receipt['evidence_path'] = str(result_path)
    receipt['independently_accepted'] = False
    return receipt


async def call_tool(name, arguments):
    try:
        result = await asyncio.to_thread(execute, name, arguments or {})
        value = {'ok':True,'result':result}
    except Exception as e:
        # No raw transport errors, response bodies, headers or tokens in MCP output.
        value = {'ok':False,'error_type':type(e).__name__,
                 'message':str(e)[:350] if isinstance(e, ProjectBlocked) else 'Project operation failed; inspect bounded local status'}
    return [TextContent(type='text',text=json.dumps(value,ensure_ascii=False))]


async def _list_tools(context, params):
    return ListToolsResult(tools=await list_tools())


async def _call_tool(context, params):
    return CallToolResult(content=await call_tool(params.name, params.arguments))


server = Server('workbuddy_video_edit', version='0.2.0', on_list_tools=_list_tools, on_call_tool=_call_tool)


async def run_server():
    runtime, _ = binding()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, InitializationOptions(server_name='workbuddy_video_edit',
                server_version='0.2.0', capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),experimental_capabilities={})))
    finally:
        await asyncio.to_thread(runtime.close)


def main():
    asyncio.run(run_server())


if __name__ == '__main__':
    main()
