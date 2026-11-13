"""Fixed project MCP entry; no cwd/session arguments accepted from task text."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operation', choices=['status','pair','run','cancel','recover','reset'])
    parser.add_argument('--event-id', type=int)
    parser.add_argument('--policy-file')
    parser.add_argument('--confirm')
    args = parser.parse_args()
    from workbuddy_acp_bridge.server import main as serve, execute, ROOT
    if args.operation is None:
        serve()
        return
    arguments = {}
    if args.operation in {'pair','reset'}:
        arguments['confirm'] = args.confirm
    if args.operation == 'run':
        from project_sync import safe
        policy = safe(ROOT, Path(args.policy_file or ''))
        with policy.open('rb') as f:
            raw = f.read(65537)
        if len(raw) > 65536:
            raise ValueError('Policy too large')
        arguments = json.loads(raw)
        arguments['event_id'] = args.event_id
    try:
        print(json.dumps(execute('workbuddy_'+args.operation, arguments),ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'status':'blocked','error_type':type(e).__name__},ensure_ascii=False))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
