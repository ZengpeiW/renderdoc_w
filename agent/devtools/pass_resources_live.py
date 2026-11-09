"""Validate pass-resource usage metadata in an owned hidden RenderTest process.

Examples:
  python agent/devtools/pass_resources_live.py --launch E:/Captures/pragmata1.rdc --parent-event 7726
  python agent/devtools/pass_resources_live.py --launch E:/Captures/恋与深空尝试截帧.rdc --parent-event 2712

The native reference reads texture/buffer GetUsage metadata once, with resource/record/byte
budgets. Neither the reference nor production tool may move replay. Only this runner's
hidden Popen is closed, and its configuration writer is disabled before capture load.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from decompile_live import AGENT_DIR, REPO_DIR, Check as BaseCheck, digest, error_text, require
from rendertest_mcp.ipc_client import BridgeClient


class Check(BaseCheck):
    def __init__(self, root, instance, observation_path, reference):
        super().__init__(root, instance)
        self.observation_path, self.reference = observation_path, reference
        self.nodes = {item['eventId']: item for item in reference['actions']}
        self.owners = {}
        self.real_events = set()
        self.execution_flags = {'Drawcall', 'Dispatch', 'MeshDispatch', 'DispatchRay', 'Copy', 'Resolve',
                                'Clear', 'Present', 'GenMips', 'BuildAccStruct', 'PassBoundary'}
        for item in reference['actions']:
            flags = set(item['flags'].replace('ActionFlags.', '').split('|'))
            for event in item['apiEvents']:
                self.owners[event['eventId']] = item['eventId']
                if event['name'] is not None:
                    self.real_events.add(event['eventId'])
            self.owners[item['eventId']] = item['eventId']
            fake = len(item['apiEvents']) == 1 and item['apiEvents'][0]['chunkIndex'] == 0xffffffff
            if not fake and flags.intersection(self.execution_flags) and not flags.intersection({'PushMarker', 'PopMarker', 'SetMarker'}):
                self.real_events.add(item['eventId'])
        self.resources = {item['resourceId']: item for item in reference['resources']}

    def observations(self):
        return json.loads(self.observation_path.read_text(encoding='utf-8'))['batches']

    def counts(self):
        return Counter(call['resourceId'] for batch in self.observations() for call in batch['usageCalls'])

    def expected_scope(self, parent=None, start=None, end=None):
        if parent is None:
            count = len(self.nodes) if start is None else sum(start <= eid <= end and eid in self.real_events for eid in self.nodes)
            selected = len(self.real_events) if start is None else sum(start <= eid <= end for eid in self.real_events)
            return {'members': None, 'start': start, 'end': end, 'actionCount': count, 'selectedEventCount': selected}
        nodes, pending = [], [parent]
        while pending:
            item = self.nodes[pending.pop()]
            nodes.append(item)
            pending.extend(reversed(item['children']))
        anchors = [item['eventId'] for item in nodes if item['eventId'] in self.real_events]
        require(anchors, 'Selected native scope has no real action anchors.')
        low, high = min(anchors), max(anchors)
        members = {event['eventId'] for item in nodes for event in item['apiEvents']}
        members.update(item['eventId'] for item in nodes)
        members = {eid for eid in members if low <= eid <= high and eid in self.real_events}
        return {'members': members, 'start': low, 'end': high, 'actionCount': len(nodes), 'selectedEventCount': len(members)}

    @staticmethod
    def access(usage):
        if usage in {'VertexBuffer', 'IndexBuffer', 'Indirect', 'InputTarget', 'CopySrc', 'ResolveSrc'} or \
                usage.endswith('_Constants') or (usage.endswith('_Resource') and not usage.endswith('_RWResource')):
            return 'read'
        if usage in {'StreamOut', 'ColorTarget', 'Clear', 'CopyDst', 'ResolveDst', 'CPUWrite'}:
            return 'write'
        if usage.endswith('_RWResource') or usage in {'Copy', 'Resolve', 'GenMips', 'DepthStencilTarget'}:
            return 'readWrite'
        if usage == 'Barrier':
            return 'barrier'
        if usage == 'Discard':
            return 'discard'
        if usage == 'Unused':
            return 'unused'
        return 'unknown'

    @staticmethod
    def inside(usage, scope):
        eid = usage['eventId']
        if scope['members'] is not None:
            return eid in scope['members']
        return scope['start'] is None or scope['start'] <= eid <= scope['end']

    def expected(self, parent=None, start=None, end=None, kind='all'):
        scope, rows = self.expected_scope(parent, start, end), []
        candidates = sorted((item for item in self.resources.values() if kind == 'all' or item['kind'] == kind),
                            key=lambda item: int(item['resourceId']))
        for resource in candidates:
            raw = sorted((item for item in resource['usages'] if not (item['eventId'] == 0 and item['usage'] == 'Unused')),
                         key=lambda item: (item['eventId'], item['usageValue'], item.get('viewId', '')))
            usages = [item for item in raw if self.inside(item, scope)]
            if not usages:
                continue
            before = [item for item in raw if scope['start'] is not None and item['eventId'] < scope['start']]
            after = [item for item in raw if scope['end'] is not None and item['eventId'] > scope['end']]
            previous = max((item['eventId'] for item in before), default=None)
            following = min((item['eventId'] for item in after), default=None)
            rows.append({'resource': resource, 'usages': usages,
                         'previous': [item for item in before if item['eventId'] == previous],
                         'next': [item for item in after if item['eventId'] == following]})
        return rows, candidates, scope

    def example(self, raw):
        return dict(raw, access=self.access(raw['usage']), actionEventId=self.owners.get(raw['eventId']))

    def verify_row(self, row, native):
        resource, usages = native['resource'], native['usages']
        for key in ('resourceId', 'name', 'type', 'kind'):
            require(row[key] == resource[key], 'Native resource metadata differs: ' + key)
        eids = [item['eventId'] for item in usages]
        require(row['usageRecordCount'] == len(usages) and row['eventCount'] == len(set(eids)) and
                row['firstEventId'] == min(eids) and row['lastEventId'] == max(eids),
                'Native scope record/event counts or EID bounds differ.')
        groups = defaultdict(list)
        for item in usages:
            groups[(item['usageValue'], item['usage'])].append(item['eventId'])
        types = [{'usageValue': value, 'usage': name, 'access': self.access(name), 'count': len(ids),
                  'firstEventId': min(ids), 'lastEventId': max(ids)} for (value, name), ids in sorted(groups.items())]
        require(row['usageTypes'] == types, 'Native usage role aggregation or duplicate counting differs.')
        require(row['hasReadUsage'] == any(item['access'] in ('read', 'readWrite') for item in types) and
                row['hasWriteUsage'] == any(item['access'] in ('write', 'readWrite') for item in types),
                'Potential resource role flags differ.')
        require(row['usageExamples'] == [self.example(item) for item in usages[:16]] and
                row['usageExamplesTruncated'] == (len(usages) > 16), 'Bounded raw usage examples differ.')
        for key, raw in (('previousUsage', native['previous']), ('nextUsage', native['next'])):
            expected = None if not raw else {'eventId': raw[0]['eventId'], 'recordCount': len(raw),
                                             'usages': [self.example(item) for item in raw[:16]], 'truncated': len(raw) > 16}
            require(row[key] == expected, 'Nearest raw usage EID/tied records differ: ' + key)

    def diagnostics(self, candidates, scope):
        counts = dict(unknownUsageRecordCount=0, unmappedUsageRecordCount=0,
                      malformedUsageRecordCount=0, unusedSentinelCount=0)
        for resource in candidates:
            for row in resource['usages']:
                if row['eventId'] == 0 and row['usage'] == 'Unused':
                    counts['unusedSentinelCount'] += 1
                    continue
                selected = self.inside(row, scope)
                counts['unknownUsageRecordCount'] += int(selected and self.access(row['usage']) == 'unknown')
                relevant = selected or (scope['members'] is not None and scope['start'] <= row['eventId'] <= scope['end'])
                counts['unmappedUsageRecordCount'] += int(relevant and row['eventId'] not in self.owners)
        return counts

    def collect(self, parent=None, start=None, end=None, kind='all', limit=3, scan_budget=127, refresh=False,
                stop_after=None):
        expected, candidates, scope = self.expected(parent, start, end, kind)
        pages, rows, cursor, processed, scan, started = [], [], None, 0, None, None
        counts_before = self.counts()
        for page_number in range(512):
            response = self.call('get_pass_resources', parent_event_id=parent, start_event_id=start, end_event_id=end,
                                 kind=kind, limit=limit, scan_budget=scan_budget, cursor=cursor,
                                 refresh=refresh and page_number == 0)
            data, coverage = response['data'], response['data']['coverage']
            require(response['effect'] == 'read' and 'replayEventRestored' not in response['meta'],
                    'Metadata query unexpectedly reported replay work.')
            if scan is None:
                scan, started = data['scanId'], data['scanStartedAtUtc']
            require((data['scanId'], data['scanStartedAtUtc']) == (scan, started), 'Pages mixed cache generations.')
            actual_scope = data['scope']
            require(actual_scope['kind'] == ('actionSubtree' if parent is not None else 'eventRange' if start is not None else 'capture'),
                    'Scope mode changed.')
            for key, value in (('parentEventId', parent), ('startEventId', scope['start']), ('endEventId', scope['end']),
                               ('actionCount', scope['actionCount']), ('selectedEventCount', scope['selectedEventCount'])):
                require(actual_scope[key] == value, 'Independent real-API scope differs: ' + key)
            if parent is not None:
                events = self.nodes[parent]['apiEvents']
                require(actual_scope['syntheticParent'] == (len(events) == 1 and events[0]['chunkIndex'] == 0xffffffff),
                        'Synthetic marker identity differs from native NoChunk metadata.')
            examined = coverage['examinedThisPage']
            require(0 <= examined <= scan_budget and examined == coverage['nativeReadsThisPage'] + coverage['cacheHitsThisPage'],
                    'Candidate scan budget/native cache accounting differs.')
            processed += examined
            require(coverage['processedResourceCount'] == processed and coverage['candidateResourceCount'] == len(candidates),
                    'Resource scan prefix/count differs.')
            native_diagnostics = self.diagnostics(candidates[:processed], scope)
            require(all(coverage[key] == value for key, value in native_diagnostics.items()),
                    'Cumulative native metadata diagnostics differ.')
            complete = processed == len(candidates)
            require(coverage['scanComplete'] == complete and coverage['complete'] ==
                    (complete and not any(native_diagnostics[key] for key in native_diagnostics if key != 'unusedSentinelCount')),
                    'Metadata coverage completeness differs.')
            require(len(data['resources']) <= limit and data['kind'] == kind, 'Resource kind/row limit changed.')
            rows.extend(data['resources'])
            pages.append(response)
            if response['nextCursor'] is None:
                require(complete, 'Resource scan ended early.')
                break
            require(not complete and response['nextCursor'] != cursor, 'Resource cursor did not advance correctly.')
            cursor = response['nextCursor']
            if stop_after is not None and len(pages) >= stop_after:
                break
        else:
            raise RuntimeError('Bounded resource pagination did not finish.')
        compared = expected if stop_after is None else expected[:len(rows)]
        require([row['resourceId'] for row in rows] == [item['resource']['resourceId'] for item in compared],
                'Matching resource IDs/order differ from independent native GetUsage.')
        for row, native in zip(rows, compared):
            self.verify_row(row, native)
        counts_after = self.counts()
        return {'parentEventId': parent, 'startEventId': start, 'endEventId': end, 'kind': kind, 'scanId': scan,
                'pages': pages, 'resources': rows, 'nativeReferenceMatched': True,
                'readAllPages': pages[-1]['nextCursor'] is None,
                'nativeReads': sum(page['data']['coverage']['nativeReadsThisPage'] for page in pages),
                'cacheHits': sum(page['data']['coverage']['cacheHitsThisPage'] for page in pages),
                'observedGetUsageBefore': sum(counts_before.values()), 'observedGetUsageAfter': sum(counts_after.values())}

    def rejected(self, code, **params):
        before = self.counts()
        try:
            self.call('get_pass_resources', **params)
        except Exception as error:
            message = error_text(error)
            require(code in message, 'Unexpected pass-resource rejection: ' + message)
            require(self.counts() == before, 'Rejected parameters queried native resource usage.')
            return {'params': params, 'error': message}
        raise RuntimeError('Invalid pass-resource request was accepted.')

    def verify_observer(self):
        batches = self.observations()
        require(batches, 'Production GetUsage was not observed.')
        for batch in batches:
            require(not batch['frameEvents'], 'Metadata query attempted SetFrameEvent.')
            require(batch['pixelsBefore'] == batch['pixelsAfter'] == self.reference['pixelsBefore'],
                    'Native metadata query changed the guard pixel.')
            require(batch['stateBefore'] == batch['stateAfter'] == self.reference['pipelineBefore'],
                    'Native metadata query changed shader/output bindings.')
            require(batch['fatalBefore']['code'] == batch['fatalAfter']['code'] == 0, 'Native metadata replay health failed.')
            for call in batch['usageCalls']:
                resource = self.resources[call['resourceId']]
                require(call['recordCount'] == len(resource['usages']) and call['sha256'] == resource['usageSha256'],
                        'Production GetUsage raw result differs from independent native reference.')
        require(self.counts() == Counter({key: 2 for key in self.resources}),
                'GetUsage was not called exactly once per resource per scan generation.')
        return {'nativeGetUsageCalls': sum(self.counts().values()), 'callsPerResource': 2,
                'setFrameEventCalls': 0, 'rawNativeHashesMatched': True, 'pixelsAndPipelineUnchanged': True,
                'batches': len(batches)}

    def run(self):
        parent = self.reference['parentEventId']
        result = {'initial': self.collect(parent=parent)}
        initial = result['initial']
        require(initial['nativeReads'] == len(self.resources) and initial['cacheHits'] == 0 and
                initial['observedGetUsageAfter'] - initial['observedGetUsageBefore'] == len(self.resources),
                'Initial scan did not call native GetUsage exactly once per candidate resource.')
        old_cursor = initial['pages'][0]['nextCursor']
        require(old_cursor is not None, 'Initial resource query did not exercise pagination.')
        bounds = self.expected_scope(parent)
        for name, kwargs in (('textures', {'parent': parent, 'kind': 'texture'}),
                             ('buffers', {'parent': parent, 'kind': 'buffer'}),
                             ('sameBoundsRange', {'start': bounds['start'], 'end': bounds['end']}),
                             ('firstAction', {'parent': bounds['start']})):
            item = result[name] = self.collect(**kwargs)
            require(item['scanId'] == initial['scanId'] and item['nativeReads'] == 0 and
                    item['observedGetUsageBefore'] == item['observedGetUsageAfter'],
                    'Scope/kind query failed to reuse the shared raw-usage cache.')
        capture = result['capturePrefix'] = self.collect(stop_after=1)
        require(capture['resources'] and capture['nativeReads'] == 0 and capture['scanId'] == initial['scanId'] and
                all(row['previousUsage'] is None and row['nextUsage'] is None for row in capture['resources']),
                'Capture-wide prefix should reuse cache and have no outside neighbours.')
        used_eids = {row['eventId'] for resource in self.resources.values() for row in resource['usages']}
        empty_eid = next(eid for eid in sorted(self.real_events) if eid not in used_eids)
        empty = result['emptyRange'] = self.collect(start=empty_eid, end=empty_eid)
        require(not empty['resources'] and empty['pages'][0]['nextCursor'] and
                not empty['pages'][0]['data']['coverage']['scanComplete'] and empty['nativeReads'] == 0,
                'Empty partial resource page was treated as a complete scan.')
        # Use the first real setup usage just outside the parent's lower anchor, if present.
        subtree_nodes, pending = [], [parent]
        while pending:
            item = self.nodes[pending.pop()]
            subtree_nodes.append(item)
            pending.extend(item['children'])
        owned = {event['eventId'] for item in subtree_nodes for event in item['apiEvents']}
        leaks = [(resource['resourceId'], row) for resource in self.resources.values() for row in resource['usages']
                 if row['eventId'] in owned and row['eventId'] < bounds['start'] and row['eventId'] in self.real_events]
        if leaks:
            key, usage = max(leaks, key=lambda item: item[1]['eventId'])
            boundary = result['setupBoundaryRange'] = self.collect(start=usage['eventId'], end=bounds['start'])
            row = next(item for item in boundary['resources'] if item['resourceId'] == key)
            require(any(item['eventId'] == usage['eventId'] and item['usageValue'] == usage['usageValue']
                        for item in row['usageExamples']), 'Raw setup usage was lost in an explicit API interval.')
            require(all(item['eventId'] >= bounds['start'] for row in initial['resources'] for item in row['usageExamples']),
                    'Pre-scope setup usage leaked in through action ownership.')
            result['boundaryEvidence'] = {'resourceId': key, 'rawUsage': usage, 'ownerActionEventId': self.owners[usage['eventId']],
                                          'excludedFromParent': True, 'includedInExplicitRange': True}
        else:
            result['boundaryEvidence'] = {'preAnchorNativeUsageCount': 0,
                                          'note': 'This fixture has no native usage on API setup owned by a scope action before its first anchor.'}
        result['rejections'] = [self.rejected('INVALID_EVENT', parent_event_id=0),
                                self.rejected('INVALID_REQUEST', parent_event_id=parent, start_event_id=bounds['start'], end_event_id=bounds['end']),
                                self.rejected('INVALID_REQUEST', start_event_id=bounds['start'])]
        if parent not in self.real_events:
            result['rejections'].append(self.rejected('INVALID_EVENT', start_event_id=parent, end_event_id=parent))
        else:
            result['parentEndpointRange'] = self.collect(start=parent, end=parent)
        refreshed = result['refreshed'] = self.collect(parent=parent, refresh=True)
        require(refreshed['scanId'] != initial['scanId'] and refreshed['nativeReads'] == len(self.resources),
                'Refresh did not replace and rebuild the shared usage cache.')
        result['rejections'].append(self.rejected('INVALID_REQUEST', parent_event_id=parent, cursor=old_cursor,
                                                 limit=3, scan_budget=127))
        result['observer'] = self.verify_observer()
        return result


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--launch', type=Path, required=True)
    parser.add_argument('--exe', type=Path, default=REPO_DIR / 'x64' / 'Release' / 'qrendertest.exe')
    parser.add_argument('--parent-event', type=int, required=True)
    parser.add_argument('--guard-resource', help='Defaults to a native current output texture, or the first 2D texture.')
    parser.add_argument('--guard-point', type=int, nargs=2, metavar=('X', 'Y'), help='Defaults to the selected texture center.')
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--wait', type=float, default=240)
    args = parser.parse_args()
    if os.name != 'nt' or args.parent_event < 1 or not 0 < args.wait <= 1800:
        parser.error('Windows, a positive parent EID and wait in (0,1800] are required.')
    if (args.guard_resource is not None and (not args.guard_resource.isdecimal() or int(args.guard_resource) <= 0)) or \
            (args.guard_point is not None and min(args.guard_point) < 0):
        parser.error('The guard needs a positive decimal ResourceId and nonnegative coordinates.')
    for name in ('launch', 'exe'):
        value = getattr(args, name).resolve()
        if not value.is_file():
            parser.error('--%s is not an existing file: %s' % (name, value))
        setattr(args, name, value)
    return args


def main():
    args = arguments()
    root = AGENT_DIR / '.agent-data' / ('pass-resources-live-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    reference_path, observation_path = root / 'native-reference.json', root / 'native-observations.json'
    config = Path(os.environ['APPDATA']) / 'qrendertest' / 'UI.config'
    configs = {path: digest(path) for path in (config, config.with_name('DefaultLayout.config'))}
    marker, startup = root / 'config-closed.txt', root / 'startup.py'
    startup.write_text("import pathlib, sys\npyrenderdoc.Config().Close()\n"
                       "pathlib.Path(%r).write_text('closed')\n" % str(marker) +
                       "sys.path.insert(0, %r)\n" % str(AGENT_DIR / 'devtools') +
                       "from pass_resources_native import start_reference\n"
                       "start_reference(%r, %r, %r, %r, %r, %r)\n" %
                       (str(reference_path), str(observation_path), args.parent_event, args.guard_resource,
                        args.guard_point, args.probe_only), encoding='utf-8')
    environment = dict(os.environ, RENDERTEST_AGENT_ROOT=str(root / 'ipc'))
    environment.pop('RENDERTEST_AGENT_CHAT_AUTOTEST', None)
    environment.pop('RENDERTEST_AGENT_BRIDGE', None)
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags, startup_info.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 0
    report = {'passed': False, 'root': str(root), 'capture': str(args.launch), 'exe': str(args.exe),
              'parentEventId': args.parent_event, 'guardResourceId': args.guard_resource,
              'guardPoint': args.guard_point, 'probeOnly': args.probe_only, 'errors': []}
    process, check = None, None
    try:
        process = subprocess.Popen([str(args.exe), '--python', str(startup), str(args.launch)],
                                   env=environment, startupinfo=startup_info, cwd=str(args.exe.parent))
        report['pid'] = process.pid
        print('Started owned hidden RenderTest:', process.pid, 'Report:', root / 'report.json', flush=True)
        discovery, deadline, instance = BridgeClient(root=root / 'ipc', environ={}), time.monotonic() + args.wait, None
        while time.monotonic() < deadline:
            require(process.poll() is None, 'Owned RenderTest exited while loading the capture.')
            candidates = [item for item in discovery.list_instances()
                          if item.get('pid') == process.pid and item.get('captureLoaded')]
            if len(candidates) == 1 and marker.is_file() and reference_path.is_file():
                instance = candidates[0]
                break
            time.sleep(.5)
        require(instance is not None, 'Capture/reference startup timed out.')
        report['instance'], report['configWriterClosed'] = instance, marker.read_text() == 'closed'
        reference = json.loads(reference_path.read_text(encoding='utf-8'))
        report['nativeReference'] = str(reference_path)
        require('error' not in reference, reference.get('error', ''))
        report['guardResourceId'], report['guardPoint'] = reference['guardResourceId'], reference['guardPoint']
        for key in ('fatalBefore', 'fatalAfter'):
            require(reference[key]['code'] == 0, 'Native reference device error: ' + reference[key]['message'])
        require(reference['pixelsBefore'] == reference['pixelsAfter'], 'Native GetUsage changed the guard pixel.')
        require(reference['pipelineBefore'] == reference['pipelineAfter'], 'Native GetUsage changed pipeline state.')
        before = {key: reference['uiStateBefore'][key] for key in ('currentEventId', 'selectedEventId')}
        after = {key: reference['uiStateAfter'][key] for key in before}
        require(before == after and reference['setFrameEventCalls'] == 0, 'Native reference changed the replay/UI event.')
        print('Native usage reference:', reference['resourceCount'], 'resources,', reference['usageRecordCount'],
              'records,', round(reference['elapsedMs'], 1), 'ms', flush=True)
        if not args.probe_only:
            check = Check(root / 'ipc', instance, observation_path, reference)
            report['uiEventBefore'] = check.ui_event()
            report['validation'] = check.run()
            report['uiEventAfter'] = check.ui_event()
            require(report['uiEventAfter'] == report['uiEventBefore'], 'Pass-resource query changed the UI selection.')
            report['nativeObservations'] = str(observation_path)
            print('Native usage records, cache and unchanged native resource state passed.', flush=True)
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
        report['configPreserved'] = all(item['before'] == item['after'] for item in report['configuration'].values())
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
