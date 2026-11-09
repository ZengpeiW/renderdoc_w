"""Validate API parameters and event comparison against an owned, hidden RenderTest.

Example:
  python agent/devtools/event_inspection_live.py --launch E:/Captures/pragmata1.rdc \
      --before 3803 --after 3820 --cbuffer-stage pixel --cbuffer-name SceneInfo \
      --member viewProjMat

No model/service is called. A separate native callback supplies APIEvent/SDObject reference
values. MCP tools are exercised with real FastMCP argument validation and fixed capture binding.
Reports are kept in agent/.agent-data/event-inspection-live-*. Existing GUI windows are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from decompile_live import AGENT_DIR, REPO_DIR, digest, error_text, require, structured
from rendertest_mcp.ipc_client import BridgeClient
from rendertest_mcp.protocol import RESULT_SCHEMA, TOOL_NAMES
from rendertest_mcp.server import create_server


class Check:
    def __init__(self, root, instance):
        self.instance = instance['instanceId']
        self.session = instance['captureSessionId']
        self.server = create_server(BridgeClient(root=root, environ={
            'RENDERTEST_AGENT_FIXED_INSTANCE_ID': self.instance,
            'RENDERTEST_AGENT_CAPTURE_SESSION_ID': self.session,
        }))
        self.timings = []
        names = {tool.name for tool in asyncio.run(self.server.list_tools())}
        require(names == set(TOOL_NAMES), 'MCP catalog does not match the protocol.')

    def call(self, method, **params):
        started = time.monotonic()
        try:
            value = structured(asyncio.run(self.server.call_tool(method, params)))
            require(value.get('schema') == RESULT_SCHEMA, method + ': invalid schema')
            require(value.get('instanceId') == self.instance, method + ': instance changed')
            require(value.get('captureSessionId') == self.session, method + ': capture changed')
            return value
        finally:
            self.timings.append({'method': method, 'params': params,
                                 'elapsedMs': round((time.monotonic() - started) * 1000, 1)})

    def ui_event(self):
        context = self.call('get_context')['data']
        return {key: context[key] for key in ('selectedEventId', 'currentEventId')}

    def pages(self, name, key, **params):
        rows, pages, cursor = [], [], None
        for _ in range(1000):
            value = self.call(name, cursor=cursor, **params)
            pages.append(value)
            rows.extend(value['data'][key])
            next_cursor = value['nextCursor']
            if next_cursor is None:
                return rows, pages
            require(next_cursor != cursor, 'Cursor did not advance.')
            cursor = next_cursor
        raise RuntimeError('Pagination did not terminate.')

    def api_calls(self, reference):
        rows, pages = self.pages('get_api_calls', 'calls', event_id=reference['eventId'], limit=3)
        expected = reference['calls']
        require(len(rows) == len(expected), 'Native API call count differs.')
        for actual, native in zip(rows, expected):
            for key in ('apiEventId', 'chunkIndex', 'available'):
                require(actual[key] == native[key], 'Native call metadata differs: ' + key)
            if native['available']:
                require(actual['name'] == native['name'], 'Native API name differs.')
                require(actual['parameterCount'] == native['parameterCount'],
                        'Native parameter count differs.')
        require(all(page['effect'] == 'read' for page in pages), 'API effect is not read.')
        non_action = next((row for row in rows if row['apiEventId'] != reference['eventId']), None)
        if non_action:
            owner = self.call('get_api_calls', event_id=non_action['apiEventId'], limit=1)['data']
            require(owner['eventId'] == non_action['apiEventId'] and
                    owner['actionEventId'] == reference['eventId'], 'API/owner EIDs were conflated.')
        samples = []
        for sample in reference['samples']:
            parameters, parameter_pages = self.pages('get_api_calls', 'parameters',
                api_event_id=sample['apiEventId'], event_id=sample['actionEventId'], limit=1)
            require(len(parameters) == parameter_pages[0]['data']['parameterCount'],
                    'Root parameter pagination lost a field.')
            leaves = []
            for leaf in sample['leaves']:
                value = self.call('get_api_calls', api_event_id=sample['apiEventId'],
                                  parameter_path=leaf['path'])
                selected = value['data']['selected']
                require(selected['canonicalPath'] == leaf['path'], 'Parameter path changed.')
                require(selected['name'] == leaf['name'], 'Native parameter name differs.')
                require(selected.get('value') == leaf['value'],
                        'Native scalar differs: %s EID %s %s' %
                        (sample['kind'], sample['apiEventId'], leaf['path']))
                leaves.append({'reference': leaf, 'selected': selected})
            arrays = []
            for native_array in sample['arrays'][:2]:
                children, array_pages = self.pages('get_api_calls', 'parameters',
                    api_event_id=sample['apiEventId'], parameter_path=native_array['path'], limit=1)
                require(len(children) == native_array['childCount'], 'Native array pagination differs.')
                arrays.append({'reference': native_array, 'pages': len(array_pages)})
            samples.append({'kind': sample['kind'], 'apiEventId': sample['apiEventId'],
                            'name': sample['name'], 'leaves': leaves, 'arrays': arrays})
        return {'calls': rows, 'pages': len(pages), 'samples': samples}

    def compare(self, args):
        params = {'before_event_id': args.before, 'after_event_id': args.after,
                  'stages': ['vertex', 'pixel'], 'include_bindings': False}
        same = self.call('compare_events', **dict(params, before_event_id=args.after))
        require(same['data']['totalDifferences'] == 0, 'Same event has spurious differences.')
        require(same['data']['comparison'] == 'equal-in-scope', 'Same event scope is incomplete.')
        rows, pages = self.pages('compare_events', 'differences', **params, limit=2)
        require(rows and len(rows) == pages[0]['data']['totalDifferences'],
                'Distinct event comparison did not yield the complete differences.')
        require(pages[0]['data']['comparison'] == 'different', 'Distinct events reported equal.')
        whole = self.call('compare_events', **params, limit=1000)
        require(rows == whole['data']['differences'], 'Difference paging changed its contents.')
        before = self.call('get_pipeline_state', event_id=args.before,
                           stages=params['stages'], include_bindings=False)['data']
        after = self.call('get_pipeline_state', event_id=args.after,
                          stages=params['stages'], include_bindings=False)['data']
        for row in rows:
            path = row['path']
            require(path.startswith('/pipeline/'), 'Unexpected difference outside pipeline.')
            parts = [value.replace('~1', '/').replace('~0', '~') for value in path.split('/')[2:]]
            for side, state in (('before', before), ('after', after)):
                present, value = True, state
                for part in parts:
                    try:
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    except (KeyError, IndexError, TypeError):
                        present, value = False, None
                        break
                require(row[side]['present'] == present, 'Missing-value diff disagrees with pipeline.')
                if present and 'valueType' in row[side]:
                    actual_type = 'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else None
                    require(row[side]['valueType'] == actual_type, 'Container type change is incorrect.')
                elif present:
                    require(row[side]['value'] == value, 'Diff value disagrees with independent pipeline read.')
        limited = self.call('compare_events', **dict(params, include_bindings=True), binding_limit=1)
        require(not limited['data']['coverage']['complete'] and limited['truncated'],
                'Small binding limit failed to report incomplete coverage.')
        partial_same = self.call('compare_events',
            **dict(params, before_event_id=args.after, include_bindings=True), binding_limit=1)
        require(partial_same['data']['comparison'] == 'indeterminate',
                'Incomplete same-event state was reported equal.')
        selection = {'stage': args.cbuffer_stage, 'name': args.cbuffer_name,
                     'member_path': args.member, 'max_depth': 0}
        cbuffer = self.call('get_cbuffer', event_id=args.after, stage=args.cbuffer_stage,
                            name=args.cbuffer_name, member_path=args.member)['data']
        cbuffer_before = self.call('get_cbuffer', event_id=args.before, stage=args.cbuffer_stage,
                                   name=args.cbuffer_name, member_path=args.member)['data']
        cb_same = self.call('compare_events', **dict(params, before_event_id=args.after),
                            cbuffers=[selection])
        require(cb_same['data']['comparison'] == 'equal-in-scope' and
                cb_same['data']['totalDifferences'] == 0, 'Identical CBuffer member did not compare equal.')
        cb_pair = self.call('compare_events', **params, cbuffers=[selection], limit=1000)
        require(cb_pair['data']['coverage']['complete'] and cb_pair['nextCursor'] is None,
                'Selected CBuffer comparison was incomplete.')
        cb_rows = [row for row in cb_pair['data']['differences'] if row['path'].startswith('/cbuffers/0/')]
        # Shader/binding/layout identity must participate, even when the selected value is equal.
        expected_identities = ('shader/resourceId', 'binding/resourceId', 'binding/byteOffset', 'index')
        for suffix in expected_identities:
            old, new = cbuffer_before, cbuffer
            for part in suffix.split('/'):
                old, new = old[part], new[part]
            matches = [row for row in cb_rows if row['path'] == '/cbuffers/0/' + suffix]
            require(bool(matches) == (old != new), 'CBuffer identity difference was lost or invented.')
        for row in cb_rows:
            parts = [part.replace('~1', '/').replace('~0', '~') for part in row['path'].split('/')[3:]]
            if parts[0] == 'projection':
                parts = parts[1:]
            for side, reference in (('before', cbuffer_before), ('after', cbuffer)):
                present, value = True, reference
                for part in parts:
                    try:
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    except (KeyError, IndexError, TypeError):
                        present, value = False, None
                        break
                require(row[side]['present'] == present, 'CBuffer presence disagrees with get_cbuffer.')
                if present and 'valueType' in row[side]:
                    actual_type = 'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else None
                    require(row[side]['valueType'] == actual_type, 'CBuffer container type change is incorrect.')
                elif present and 'value' in row[side]:
                    require(row[side]['value'] == value, 'CBuffer diff disagrees with get_cbuffer.')
        old_value, new_value = cbuffer_before['selected']['value'], cbuffer['selected']['value']
        value_rows = [row for row in cb_rows if row['path'].startswith('/cbuffers/0/projection/selected/value')]
        require(bool(value_rows) == (old_value != new_value), 'CBuffer member value change was lost or invented.')
        for page in [same, whole, limited, partial_same, cb_same, cb_pair] + pages:
            require(page['effect'] == 'read', 'Comparison effect is not read.')
            require(page['meta'].get('replayEventRestored') is True, 'Comparison did not restore replay.')
        return {'same': same, 'different': whole, 'differencePages': len(pages),
                'pipelineBefore': before, 'pipelineAfter': after, 'bindingLimit': limited,
                'incompleteSame': partial_same, 'cbufferBeforeReference': cbuffer_before,
                'cbufferReference': cbuffer, 'cbufferSame': cb_same, 'cbufferPair': cb_pair,
                'cbufferSelectedValuesEqual': old_value == new_value}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--launch', type=Path, required=True)
    parser.add_argument('--exe', type=Path, default=REPO_DIR / 'x64/Release/qrendertest.exe')
    parser.add_argument('--before', type=int, required=True)
    parser.add_argument('--after', type=int, required=True)
    parser.add_argument('--cbuffer-stage', default='pixel')
    parser.add_argument('--cbuffer-name', required=True)
    parser.add_argument('--member', required=True)
    parser.add_argument('--wait', type=float, default=240)
    args = parser.parse_args()
    if os.name != 'nt' or min(args.before, args.after) < 0 or not 0 < args.wait <= 1800:
        parser.error('Requires Windows, nonnegative EIDs and --wait in (0,1800].')
    for name in ('launch', 'exe'):
        value = getattr(args, name).resolve()
        if not value.is_file():
            parser.error('--%s is not an existing file' % name)
        setattr(args, name, value)
    return args


def main():
    args = arguments()
    root = AGENT_DIR / '.agent-data' / ('event-inspection-live-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    reference = root / 'native-reference.json'
    marker = root / 'config-closed.txt'
    startup = root / 'startup.py'
    startup.write_text(
        'import pathlib, sys\npyrenderdoc.Config().Close()\n'
        'pathlib.Path(%r).write_text("closed")\nsys.path.insert(0, %r)\n'
        'from event_inspection_native import start_reference\nstart_reference(%r, %d)\n' %
        (str(marker), str(AGENT_DIR / 'devtools'), str(reference), args.after), encoding='utf-8')
    config = Path(os.environ['APPDATA']) / 'qrendertest/UI.config'
    configs = {path: digest(path) for path in (config, config.with_name('DefaultLayout.config'))}
    environment = dict(os.environ, RENDERTEST_AGENT_ROOT=str(root / 'ipc'))
    environment.pop('RENDERTEST_AGENT_CHAT_AUTOTEST', None)
    environment.pop('RENDERTEST_AGENT_BRIDGE', None)
    info = subprocess.STARTUPINFO()
    info.dwFlags = subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    report = {'passed': False, 'root': str(root), 'capture': str(args.launch), 'exe': str(args.exe),
              'beforeEventId': args.before, 'afterEventId': args.after, 'errors': []}
    process = check = None
    try:
        process = subprocess.Popen([str(args.exe), '--python', str(startup), str(args.launch)],
                                   env=environment, startupinfo=info, cwd=str(args.exe.parent))
        report['pid'] = process.pid
        print('Started owned hidden RenderTest:', process.pid, 'Report:', root / 'report.json', flush=True)
        discovery = BridgeClient(root=root / 'ipc', environ={})
        deadline, instance = time.monotonic() + args.wait, None
        while time.monotonic() < deadline:
            require(process.poll() is None, 'Owned RenderTest exited during startup.')
            candidates = [item for item in discovery.list_instances()
                          if item.get('pid') == process.pid and item.get('captureLoaded')]
            if len(candidates) == 1 and marker.is_file() and reference.is_file():
                instance = candidates[0]
                break
            time.sleep(.5)
        require(instance is not None, 'Capture/reference startup timed out.')
        report.update(instance=instance, configWriterClosed=True, toolCount=len(TOOL_NAMES))
        native = json.loads(reference.read_text(encoding='utf-8'))
        require('error' not in native, str(native.get('error')))
        report['nativeReference'] = native
        check = Check(root / 'ipc', instance)
        before = check.ui_event()
        report['uiEventBefore'] = before
        report['apiCalls'] = check.api_calls(native)
        print('API calls, typed parameters and arrays matched native reference.', flush=True)
        report['comparison'] = check.compare(args)
        report['uiEventAfter'] = check.ui_event()
        require(report['uiEventAfter'] == before, 'Tools changed the UI event.')
        report['uiEventPreserved'] = True
    except BaseException as error:
        report['errors'].append(error_text(error))
    finally:
        if check is not None:
            report['calls'] = check.timings
        if process is not None:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
            report['ownedProcessExited'] = process.poll() is not None
        report['configuration'] = {str(path): {'before': old, 'after': digest(path)}
                                   for path, old in configs.items()}
        report['configPreserved'] = all(value['before'] == value['after']
                                        for value in report['configuration'].values())
        if not report['configPreserved']:
            report['errors'].append('UI.config or DefaultLayout.config changed.')
        report['passed'] = not report['errors'] and bool(report.get('ownedProcessExited'))
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
        print('Report:', root / 'report.json', 'passed=' + str(report['passed']).lower(), flush=True)
        if report['errors']:
            print(json.dumps(report['errors'], ensure_ascii=True), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
