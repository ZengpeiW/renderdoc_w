"""Offline package check. Never reads provider configuration or sends a model request."""
from pathlib import Path
import argparse
import hashlib
import importlib
import importlib.metadata
import json
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--verify-files', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        for module in ('anyio', 'mcp', 'pydantic', 'claude_agent_sdk', 'rendertest_mcp.server', 'rendertest_mcp.claude_backend'):
            imported = importlib.import_module(module)
            if not Path(imported.__file__).resolve().is_relative_to(root):
                raise RuntimeError('Dependency loaded outside this package: ' + module)
        from rendertest_mcp.protocol import TOOL_NAMES
        if len(TOOL_NAMES) != 35:
            raise RuntimeError('Unexpected tool catalog size: ' + str(len(TOOL_NAMES)))
        integration = root / 'integrations/rendertest_agent'
        names = ('rendertest-core', 'rendertest-event-analysis', 'rendertest-shader-debug', 'rendertest-pixel-investigation')
        for name in names:
            if not (integration / 'skills' / name / 'SKILL.md').is_file():
                raise RuntimeError('Missing Skill: ' + name)
        if not (root / 'qrendertest.exe').is_file() or not (root / 'rendertest.dll').is_file():
            raise RuntimeError('Incomplete native application files')
        for abi in ('arm64', 'arm32'):
            apk = root / 'plugins/android' / f'org.renderdoc.renderdoccmd.{abi}.apk'
            if not apk.is_file() or apk.stat().st_size == 0:
                raise RuntimeError('Missing Android capture server: ' + apk.name)
        if args.verify_files:
            manifest = json.loads((root / 'PACKAGE_MANIFEST.json').read_text(encoding='utf-8'))
            for entry in manifest['files']:
                path = (root / entry['path']).resolve()
                if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != entry['size']:
                    raise RuntimeError('Missing or invalid package file: ' + entry['path'])
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                    raise RuntimeError('Package hash mismatch: ' + entry['path'])
        if not args.quiet:
            print('RenderTest AI Assistant package: OK')
            print('Python:', sys.version.split()[0], '| tools: 35 | Skills: 4')
            print('MCP:', importlib.metadata.version('mcp'), '| Claude SDK:', importlib.metadata.version('claude-agent-sdk'))
            for name in ('codex', 'claude'):
                print(name + ' on PATH:', shutil.which(name) or 'not found; install it or select its executable in Settings')
            print('Provider authentication was not checked. No online requests were made.')
        return 0
    except Exception as error:
        print('Package check FAILED:', type(error).__name__, str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
