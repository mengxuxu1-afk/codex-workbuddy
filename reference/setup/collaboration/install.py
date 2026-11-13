"""Install only this project's cooperation adapters; does not trust hooks for users."""
import base64
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_sync import TOOL_PATTERN, safe


def main():
    backup = safe(ROOT, ROOT / '.work/collaboration_20261109/baseline')
    backup.mkdir(parents=True, exist_ok=True)
    python = safe(ROOT, ROOT / '.work/开发环境/Scripts/python.exe')
    if not python.is_file():
        raise ValueError('本项目Python环境缺失')
    script = safe(ROOT, ROOT / 'setup/collaboration/project_sync.py')
    saved = []
    for agent, relative in [('codex', '.codex/hooks.json'), ('workbuddy', '.codebuddy/settings.json')]:
        path = safe(ROOT, ROOT / relative); path.parent.mkdir(exist_ok=True)
        if path.is_file():
            previous = backup / (agent + '_hooks.json')
            if not previous.exists():
                shutil.copy2(path, previous)
            config = json.loads(path.read_text('utf-8-sig'))
        else:
            config = {}
        hooks = config.setdefault('hooks', {})
        # The same ASCII-only adapter works through cmd.exe and Git Bash. It
        # avoids nested Windows command-line quoting and passes stdin unchanged.
        source = "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[System.Text.UTF8Encoding]::new($false); [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); & '" + str(python).replace("'", "''") + "' -X utf8 '" + str(script).replace("'", "''") + "' hook --agent " + agent + "; exit $LASTEXITCODE"
        command = 'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + base64.b64encode(source.encode('utf-16le')).decode()
        if agent == 'codex':
            handler = dict(type='command', command=command, commandWindows=command, timeout=15)
        else:
            handler = dict(type='command', command=command, timeout=15)
        for event in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'):
            existing = hooks.setdefault(event, [])
            if any(row.get('collaboration_adapter') for row in existing):
                raise ValueError('已有协作适配器，请先审核而不是覆盖')
            # Keep official schema only. Idempotence is by exact command.
            matches = [row for row in existing if any(command == h.get('command') for h in row.get('hooks', []))]
            if not matches:
                row = dict(hooks=[dict(handler)])
                if event in {'SessionStart', 'PreToolUse', 'PostToolUse'}:
                    row['matcher'] = (TOOL_PATTERN + ('|Read|read|Glob|glob|Grep|grep' if agent == 'workbuddy' else '')) if event == 'PreToolUse' else '.*'
                existing.append(row)
            elif event == 'PreToolUse' and len(matches) == 1 and len(matches[0]['hooks']) == 1:
                matches[0]['matcher'] = TOOL_PATTERN + ('|Read|read|Glob|glob|Grep|grep' if agent == 'workbuddy' else '')
        with path.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        saved.append(str(path))
    print(json.dumps(dict(configured=saved, hook_trust_changed=False,
                          global_configuration_changed=False, host_handshake_pending=True), ensure_ascii=False))


if __name__ == '__main__':
    main()
