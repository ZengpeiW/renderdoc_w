"""Validate compute debugging in an owned hidden RenderTest, without calling an AI model.

Example:
  python agent/devtools/compute_debug_live.py --launch E:/Captures/pragmata1.rdc

Use --probe-only to find a small debuggable dispatch with independent native DebugThread inputs
before deploying the MCP tool. --event restricts both the probe and validation to one dispatch.
Only the Popen process created here is closed. Its configuration writer is disabled at startup;
user UI.config/DefaultLayout.config hashes must remain unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from decompile_live import AGENT_DIR, REPO_DIR, Check as BaseCheck, digest, error_text, require
from rendertest_mcp.ipc_client import BridgeClient


class Check(BaseCheck):
    def expect_error(self, code, **params):
        try:
            self.call('debug_compute', **params)
        except Exception as error:
            text = error_text(error)
            require(code in text, 'Expected %s, got %s' % (code, text))
            return {'params': params, 'code': code, 'message': text}
        raise RuntimeError('Invalid compute invocation was accepted: %r' % params)

    def verify(self, response, reference, group, thread, max_steps, complete):
        data = response['data']
        require(response['effect'] == 'artifact', 'Compute debugging effect must be artifact.')
        require(any('UAV' in warning for warning in response['warnings']),
                'Compute trace lacks its resource-write limitation warning.')
        require(data['debugged'] is True, 'The selected compute thread was not debugged.')
        require(data['executionComplete'] is complete, 'Unexpected execution completion status.')
        require(data['traceComplete'] is True, 'Trace file reached its size budget.')
        require(0 < data['steps'] <= max_steps, 'Invalid step count.')
        require(data['eventId'] == reference['eventId'] and data['stage'] == 'compute',
                'Compute invocation identity changed.')
        require(data['group'] == group and data['thread'] == thread, 'Requested coordinates changed.')
        size, dims = reference['threadGroupSize'], reference['dispatchDimensions']
        global_thread = [group[i] * size[i] + thread[i] for i in range(3)]
        require(data['globalThread'] == global_thread, 'Global thread coordinate calculation differs.')
        require(data['threadGroupSize'] == size and data['dispatchDimensions'] == dims,
                'Dispatch dimensions disagree with independent native metadata.')
        require(data['threadGroupSizeSource'] in ('action_override', 'shader_reflection'),
                'Thread group size source is missing.')
        require(data['threadGroupSizeSource'] == reference['threadGroupSizeSource'],
                'Thread group size source disagrees with native action/reflection metadata.')
        require(data['shader']['resourceId'] == reference['shaderResourceId'], 'Shader identity differs.')
        require(data['shader']['entryPoint'] == reference['entryPoint'], 'Shader entry point differs.')
        require(response['meta'].get('replayEventRestored') is True, 'Replay EID was not restored.')

        expected = {'SV_GroupID': group, 'SV_GroupThreadID': thread,
                    'SV_DispatchThreadID': global_thread,
                    'SV_GroupIndex': [thread[0] + size[0] * (thread[1] + size[1] * thread[2])]}
        inputs = {item['name']: item for item in data['inputs']}
        checked = {}
        for name, values in expected.items():
            if name in inputs:
                require(inputs[name]['value'] == values, 'System input differs: ' + name)
                checked[name] = values
        require('SV_DispatchThreadID' in checked or
                ('SV_GroupID' in checked and 'SV_GroupThreadID' in checked),
                'No native system inputs establish both coordinate systems.')
        if group == reference['group'] and thread == reference['thread']:
            for native in reference['inputs']:
                if native['name'] in expected:
                    require(inputs[native['name']]['value'] == native['value'],
                            'Tool input differs from independent DebugThread: ' + native['name'])

        require(len(response['artifacts']) == 1, 'Expected one trace artifact.')
        artifact = response['artifacts'][0]
        path = Path(artifact['path']).resolve(strict=True)
        require(path.is_relative_to(self.scope) and path.suffix == '.jsonl', 'Trace escaped capture scope.')
        require(path.stat().st_size == artifact['bytes'] <= 64 * 1024 * 1024,
                'Trace byte count or budget is invalid.')
        require(digest(path) == artifact['sha256'], 'Trace SHA-256 mismatch.')
        require(data['traceArtifact'] == artifact['path'], 'Trace result/metadata paths differ.')
        require(artifact['kind'] == 'shader-debug-trace' and
                artifact['mediaType'] == 'application/x-ndjson', 'Wrong trace artifact type.')
        source = artifact['source']
        invocation_keys = ('eventId', 'stage', 'group', 'thread', 'globalThread',
                           'dispatchDimensions', 'threadGroupSize', 'threadGroupSizeSource')
        for key in invocation_keys:
            source_key = 'event_id' if key == 'eventId' else key
            require(source.get(source_key) == data[key], 'Trace source differs: ' + source_key)
        header_page = self.call('read_artifact', path=str(path), limit=1)['data']
        require(len(header_page['lines']) == 1, 'read_artifact did not return the header.')
        header = json.loads(header_page['lines'][0]['text'])
        require(header['record'] == 'header', 'Trace header record is missing.')
        require(header['schema'] == 'rendertest-shader-debug-trace/v1', 'Trace schema differs.')
        for key in invocation_keys:
            require(header['invocation'].get(key) == data[key], 'Trace header differs: ' + key)
        summary_page = self.call('read_artifact', path=str(path), search='"record":"summary"',
                                 limit=1)['data']
        require(len(summary_page['lines']) == 1, 'read_artifact did not find the summary.')
        summary = json.loads(summary_page['lines'][0]['text'])
        require(summary['steps'] == data['steps'] and summary['executionComplete'] is complete,
                'Trace summary does not match the result.')
        require(summary['traceComplete'] == data['traceComplete'], 'Trace completeness differs.')
        step_page = self.call('read_artifact', path=str(path), search='"record":"step"',
                              limit=2, max_bytes=128 * 1024)['data']
        require(step_page['lines'], 'read_artifact could not read a trace step.')
        with path.open('r', encoding='utf-8') as stream:
            actual_steps = sum(json.loads(line).get('record') == 'step' for line in stream)
        require(actual_steps == data['steps'], 'Trace step count differs from result.')
        if not complete:
            require(any('max_steps' in warning for warning in response['warnings']),
                    'Partial execution lacks a max_steps warning.')
        # No inference about final UAV contents is made from register changes or absent signatures.
        if reference['outputSignatureCount'] == 0:
            require(data['outputs'] == [], 'A signature-less compute shader invented output values.')
        return {'result': response, 'systemInputsChecked': checked, 'artifactHashVerified': True,
                'headerPage': header_page, 'summaryPage': summary_page, 'stepPage': step_page,
                'actualTraceSteps': actual_steps,
                'uavOutputClaim': 'Not evaluated; lastChanges are register changes, not final UAV writes.'}

    def run_matrix(self, reference, draw_event, max_steps):
        event = reference['eventId']
        common = dict(event_id=event)
        cases = []
        for group, thread, steps, complete in (
                ([0, 0, 0], [0, 0, 0], max_steps, True),
                (reference['group'], reference['thread'], max_steps, True),
                (reference['group'], reference['thread'], 1, False)):
            response = self.call('debug_compute', **common, group=group, thread=thread, max_steps=steps)
            cases.append(self.verify(response, reference, group, thread, steps, complete))
            print('Validated EID %s group=%s thread=%s steps=%s complete=%s' %
                  (event, group, thread, response['data']['steps'], complete), flush=True)
        group_bad, thread_bad = [0, 0, 0], [0, 0, 0]
        group_bad[0], thread_bad[0] = reference['dispatchDimensions'][0], reference['threadGroupSize'][0]
        rejected = [
            self.expect_error('INVALID_REQUEST', **common, group=group_bad, thread=[0, 0, 0]),
            self.expect_error('INVALID_REQUEST', **common, group=[0, 0, 0], thread=thread_bad),
            self.expect_error('INVALID_EVENT', event_id=draw_event, group=[0, 0, 0], thread=[0, 0, 0]),
        ]
        if reference['nonActionApiEventId'] is not None:
            rejected.append(self.expect_error('INVALID_EVENT', event_id=reference['nonActionApiEventId'],
                                               group=[0, 0, 0], thread=[0, 0, 0]))
        return {'cases': cases, 'rejected': rejected}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--launch', type=Path, required=True)
    parser.add_argument('--exe', type=Path, default=REPO_DIR / 'x64/Release/qrendertest.exe')
    parser.add_argument('--event', type=int)
    parser.add_argument('--draw-event', type=int, default=3820)
    parser.add_argument('--max-steps', type=int, default=20000)
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--wait', type=float, default=240)
    args = parser.parse_args()
    if os.name != 'nt' or not 0 < args.wait <= 1800 or not 1 < args.max_steps <= 200000:
        parser.error('Requires Windows, --wait in (0,1800] and --max-steps in [2,200000].')
    if args.event is not None and args.event < 0 or args.draw_event < 0:
        parser.error('Event IDs must be nonnegative.')
    for name in ('launch', 'exe'):
        path = getattr(args, name).resolve()
        if not path.is_file():
            parser.error('--%s is not an existing file' % name)
        setattr(args, name, path)
    return args


def main():
    args = arguments()
    root = AGENT_DIR / '.agent-data' / ('compute-debug-live-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    reference, marker, startup = root / 'native-reference.json', root / 'config-closed.txt', root / 'startup.py'
    startup.write_text('import pathlib, sys\npyrenderdoc.Config().Close()\n'
                       'pathlib.Path(%r).write_text("closed")\nsys.path.insert(0, %r)\n'
                       'from compute_debug_native import start_reference\nstart_reference(%r, %r)\n' %
                       (str(marker), str(AGENT_DIR / 'devtools'), str(reference), args.event), encoding='utf-8')
    config = Path(os.environ['APPDATA']) / 'qrendertest/UI.config'
    configs = {path: digest(path) for path in (config, config.with_name('DefaultLayout.config'))}
    environment = dict(os.environ, RENDERTEST_AGENT_ROOT=str(root / 'ipc'))
    environment.pop('RENDERTEST_AGENT_CHAT_AUTOTEST', None)
    environment.pop('RENDERTEST_AGENT_BRIDGE', None)
    info = subprocess.STARTUPINFO()
    info.dwFlags, info.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 0
    report = {'passed': False, 'root': str(root), 'capture': str(args.launch), 'exe': str(args.exe),
              'probeOnly': args.probe_only, 'errors': []}
    process = check = None
    try:
        process = subprocess.Popen([str(args.exe), '--python', str(startup), str(args.launch)],
                                   env=environment, startupinfo=info, cwd=str(args.exe.parent))
        report['pid'] = process.pid
        print('Started owned hidden RenderTest:', process.pid, 'Report:', root / 'report.json', flush=True)
        discovery = BridgeClient(root=root / 'ipc', environ={})
        deadline, instance = time.monotonic() + args.wait, None
        while time.monotonic() < deadline:
            require(process.poll() is None, 'Owned RenderTest exited during startup/native probe.')
            candidates = [item for item in discovery.list_instances()
                          if item.get('pid') == process.pid and item.get('captureLoaded')]
            if len(candidates) == 1 and marker.is_file() and reference.is_file():
                instance = candidates[0]
                break
            time.sleep(.5)
        require(instance is not None, 'Capture/native compute probe timed out.')
        report.update(instance=instance, configWriterClosed=True)
        native = json.loads(reference.read_text(encoding='utf-8'))
        report['nativeReference'] = native
        require('error' not in native, str(native.get('error')))
        selected = native.get('selected')
        require(selected is not None, 'No tested dispatch exposed debuggable compute coordinate inputs.')
        print('Native compute candidate:', json.dumps(selected, ensure_ascii=True), flush=True)
        if not args.probe_only:
            check = Check(root / 'ipc', instance)
            before = check.ui_event()
            report['uiEventBefore'] = before
            report['validation'] = check.run_matrix(selected, args.draw_event, args.max_steps)
            report['uiEventAfter'] = check.ui_event()
            require(report['uiEventAfter'] == before, 'Compute tools changed the UI event.')
            report['uiEventPreserved'] = True
    except BaseException as error:
        report['errors'].append(error_text(error))
    finally:
        if check is not None:
            report['calls'] = check.timings
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                report['ownedProcessExited'] = process.poll() is not None
            except BaseException as error:
                report['errors'].append('Cleanup: ' + error_text(error))
        report['configuration'] = {str(path): {'before': old, 'after': digest(path)} for path, old in configs.items()}
        report['configPreserved'] = all(value['before'] == value['after'] for value in report['configuration'].values())
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
