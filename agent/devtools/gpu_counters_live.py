"""Validate duration counters in an owned hidden RenderTest without vendor-counter sampling.

Example:
  python agent/devtools/gpu_counters_live.py --launch E:/Captures/pragmata1.rdc --probe-only
  python agent/devtools/gpu_counters_live.py --launch E:/Captures/pragmata1.rdc --event 3820 --guard-resource 390195

The native observer records the same FetchCounters result consumed by MCP, so exact comparisons
do not assume two GPU measurements are equal. Only EventGPUDuration is sampled. This runner
disables its process's config writer, hashes user configuration, and closes only its own Popen.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import subprocess
import time
import uuid

from decompile_live import AGENT_DIR, REPO_DIR, Check as BaseCheck, digest, error_text, require
from rendertest_mcp.ipc_client import BridgeClient


class Check(BaseCheck):
    def __init__(self, root, instance, measurement_path):
        super().__init__(root, instance)
        self.measurement_path = measurement_path

    def measurements(self):
        return json.loads(self.measurement_path.read_text(encoding='utf-8'))['measurements']

    def pages(self, method, field, **params):
        rows, pages, cursor = [], [], None
        for _ in range(2000):
            response = self.call(method, **params, cursor=cursor)
            require(response['effect'] == 'read', method + ' must have read effect.')
            rows.extend(response['data'][field])
            pages.append(response)
            if response['nextCursor'] is None:
                return rows, pages
            require(response['nextCursor'] != cursor, 'Pagination stopped advancing.')
            cursor = response['nextCursor']
        raise RuntimeError('Pagination did not finish.')

    def rejected(self, **params):
        try:
            self.call('get_gpu_duration', **params)
        except Exception as error:
            message = error_text(error)
            require('INVALID_REQUEST' in message, 'Unexpected error: ' + message)
            return {'params': params, 'error': message}
        raise RuntimeError('Invalid duration request was accepted.')

    def catalog(self, reference):
        actual, pages = self.pages('list_gpu_counters', 'counters', limit=3)
        expected = {item['counterId']: item for item in reference['counters']}
        require(len(actual) == len(expected), 'Native/MCP counter catalogs differ in size.')
        require(len({item['id'] for item in actual}) == len(actual), 'Catalog IDs are duplicated.')
        for item in actual:
            native = expected[item['id']]
            for key in ('name', 'category', 'description', 'resultType', 'resultByteWidth', 'unit', 'uuid'):
                require(item[key] == native[key], 'Counter descriptor differs: %s %s' % (item['id'], key))
            require(item['durationSamplingSupported'] == (item['id'] == reference['counterId']),
                    'Catalog misstates supported duration sampling.')
        require(not self.measurements(), 'Catalog enumeration unexpectedly sampled counters.')
        return {'counters': actual, 'pages': len(pages), 'nativeMetadataMatched': True}

    def verify_rows(self, rows, data, reference, measurement, scope_ids=None):
        native_actions = {item['eventId']: item for item in reference['actions']}
        scope_ids = set(native_actions) if scope_ids is None else set(scope_ids)
        require(len(rows) == len(scope_ids) == data['scopeActionCount'], 'Scope action coverage differs.')
        require({row['eventId'] for row in rows} == scope_ids, 'Scope has missing or foreign action IDs.')
        source = defaultdict(list)
        for row in measurement['rows']:
            if row['counterId'] == reference['counterId'] and row['eventId'] in native_actions:
                source[row['eventId']].append(row)
        counts = Counter()
        for row in rows:
            values = source[row['eventId']]
            expected_status = ('missing' if not values else 'duplicate' if len(values) != 1 else
                               'valid' if values[0]['valid'] else 'invalid')
            require(row['status'] == expected_status, 'Native sample status differs at EID %s' % row['eventId'])
            counts[expected_status] += 1
            if expected_status == 'valid':
                seconds = values[0]['seconds']
                require(math.isfinite(row['durationSeconds']) and row['durationSeconds'] >= 0,
                        'Duration is negative or nonfinite.')
                require(row['durationSeconds'] == seconds, 'Duration differs from the SAME native FetchCounters.')
                require(row['durationMs'] == seconds * 1000, 'Seconds-to-ms conversion differs.')
            else:
                require(row['durationSeconds'] is None and row['durationMs'] is None,
                        'Unavailable measurement was turned into zero.')
        for status in ('valid', 'invalid', 'missing', 'duplicate'):
            require(data[status + 'DurationCount'] == counts[status], 'Status coverage total differs: ' + status)
        require(data['counterId'] == reference['counterId'] and data['samplingScope'] == 'capture',
                'Sampling scope/counter metadata differs.')
        require(data['diagnostics']['rawResultCount'] == len(measurement['rows']),
                'Native raw-result coverage differs.')
        return dict(counts)

    def verify_restoration(self, measurement, event):
        require(measurement['counterIds'] == [1], 'A non-duration counter was sampled.')
        for key in ('fatalBefore', 'fatalAfterFetch', 'fatalAfterCallback'):
            require(measurement[key]['code'] == 0, 'Native replay failed: ' + measurement[key]['message'])
        require(measurement['pixelsBefore'], 'There are no actual resource-state guards.')
        require(measurement['pixelsBefore'] != measurement['pixelsAfterFetchBeforeRestore'],
                'Selected guard did not observe a resource-state change caused by FetchCounters.')
        require(measurement['pixelsRestored'], 'Resource state after callback differs from before FetchCounters.')
        require(any(move['eventId'] == event and move['force'] for move in measurement['frameEventsAfterFetch']),
                'FetchCounters was not followed by a forced replay restore.')
        return {'pixelCount': len(measurement['pixelsBefore']),
                'fetchChangedObservedPixels': measurement['pixelsBefore'] !=
                                              measurement['pixelsAfterFetchBeforeRestore'],
                'actualResourceStateRestored': True, 'forcedRestoreObserved': True}

    def duration(self, reference, event):
        rows, pages = self.pages('get_gpu_duration', 'events', limit=37, sort_by='duration')
        measurements = self.measurements()
        require(len(measurements) == 1, 'Pagination caused extra FetchCounters calls.')
        sample = pages[0]['data']['sampleId']
        require(not pages[0]['data']['cached'], 'The first duration measurement was already cached.')
        for index, page in enumerate(pages):
            require(page['data']['sampleId'] == sample, 'Pages mixed duration snapshots.')
            if index:
                require(page['data']['cached'], 'A continued page was not cached.')
        counts = self.verify_rows(rows, pages[0]['data'], reference, measurements[0])
        require(counts.get('valid', 0) > 0, 'No valid GPU durations; an all-missing sample is not successful timing validation.')
        valid = [row for row in rows if row['status'] == 'valid']
        require(valid == sorted(valid, key=lambda row: (-row['durationSeconds'], row['eventId'])),
                'Duration sorting/tie ordering differs.')
        require(rows[:len(valid)] == valid, 'Unavailable values sort before valid durations.')
        event_rows, event_pages = self.pages('get_gpu_duration', 'events', limit=200, sort_by='event_id')
        require([row['eventId'] for row in event_rows] == sorted(row['eventId'] for row in event_rows),
                'Event-ID sorting is unstable.')
        require({row['eventId']: row for row in event_rows} == {row['eventId']: row for row in rows},
                'Changing sort changed the sampled values.')
        require(all(page['data']['sampleId'] == sample for page in event_pages),
                'Sort-only request created another measurement.')
        single = self.call('get_gpu_duration', parent_event_id=event)
        self.verify_rows(single['data']['events'], single['data'], reference, measurements[0], [event])
        require(single['data']['sampleId'] == sample, 'Scope-only request created another measurement.')
        nodes = {item['eventId']: item for item in reference['actions']}
        parent = nodes[event]['parentEventId']
        subtree_result = None
        if parent is not None:
            pending, ids = [parent], []
            while pending:
                eid = pending.pop()
                ids.append(eid)
                pending.extend(nodes[eid]['children'])
            subtree_rows, subtree_pages = self.pages('get_gpu_duration', 'events', parent_event_id=parent,
                                                     limit=200, sort_by='event_id')
            self.verify_rows(subtree_rows, subtree_pages[0]['data'], reference, measurements[0], ids)
            require(all(page['data']['sampleId'] == sample for page in subtree_pages),
                    'Parent subtree changed the measurement.')
            subtree_result = {'parentEventId': parent, 'actionCount': len(ids), 'pages': len(subtree_pages)}
        require(len(self.measurements()) == 1, 'Cached scope/sort requests replayed counters.')
        restore = self.verify_restoration(measurements[0], event)
        first_cursor = pages[0]['nextCursor']
        rejected = []
        if first_cursor:
            rejected.append(self.rejected(cursor=first_cursor, sort_by='event_id'))
            rejected.append(self.rejected(cursor=first_cursor, refresh=True))
        refreshed = self.call('get_gpu_duration', refresh=True, limit=17)
        require(refreshed['data']['sampleId'] != sample and not refreshed['data']['cached'],
                'Explicit refresh did not create a new sample.')
        updated = self.measurements()
        require(len(updated) == 2, 'Refresh must fetch exactly one new duration measurement.')
        refreshed_rows, refreshed_pages = self.pages('get_gpu_duration', 'events', limit=200,
                                                     sort_by='duration')
        require(all(page['data']['sampleId'] == refreshed['data']['sampleId'] for page in refreshed_pages),
                'Refreshed pages mixed snapshots.')
        require(refreshed['data']['events'] == refreshed_rows[:len(refreshed['data']['events'])],
                'Refresh first page disagrees with its cached continuation.')
        refreshed_counts = self.verify_rows(refreshed_rows, refreshed['data'], reference, updated[1])
        require(refreshed_counts.get('valid', 0) > 0, 'Refreshed sample has no valid GPU durations.')
        require(len(self.measurements()) == 2, 'Refreshed pagination sampled counters again.')
        refreshed_restore = self.verify_restoration(updated[1], event)
        if first_cursor:
            rejected.append(self.rejected(cursor=first_cursor, sort_by='duration'))
        return {'firstPage': pages[0], 'coverageCounts': counts, 'allEvents': rows,
                'durationPages': len(pages), 'eventIdPages': len(event_pages),
                'singleAction': single, 'subtree': subtree_result, 'restoration': restore,
                'refresh': refreshed, 'refreshedRestoration': refreshed_restore, 'rejected': rejected,
                'refreshedCoverageCounts': refreshed_counts, 'refreshedAllEvents': refreshed_rows,
                'refreshedPages': len(refreshed_pages),
                'sameMeasurementExactMatch': True, 'nativeMeasurements': updated,
                'timingComparisonPolicy': 'Separate measurements may differ; no cross-sample equality asserted.'}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--launch', type=Path, required=True)
    parser.add_argument('--exe', type=Path, default=REPO_DIR / 'x64/Release/qrendertest.exe')
    parser.add_argument('--event', type=int, default=3820, help='Owned test GUI replay-state guard event.')
    parser.add_argument('--auto-event', action='store_true', help='Choose an early draw with a texture guard.')
    parser.add_argument('--guard-resource', action='append', default=[],
                        help='Bound texture ResourceId to guard; repeat for several. Checks remain bit exact.')
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--wait', type=float, default=240)
    args = parser.parse_args()
    if os.name != 'nt' or args.event < 0 or not 0 < args.wait <= 1800:
        parser.error('Requires Windows, nonnegative --event, and --wait in (0,1800].')
    if any(not value.isascii() or not value.isdigit() or int(value) <= 0 for value in args.guard_resource):
        parser.error('--guard-resource must be a positive decimal ResourceId.')
    for name in ('launch', 'exe'):
        path = getattr(args, name).resolve()
        if not path.is_file():
            parser.error('--%s is not an existing file' % name)
        setattr(args, name, path)
    if args.auto_event:
        args.event = None
    return args


def main():
    args = arguments()
    root = AGENT_DIR / '.agent-data' / ('gpu-counters-live-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    reference, observations = root / 'native-reference.json', root / 'native-measurements.json'
    marker, startup = root / 'config-closed.txt', root / 'startup.py'
    startup.write_text('import pathlib, sys\npyrenderdoc.Config().Close()\n'
                       'pathlib.Path(%r).write_text("closed")\nsys.path.insert(0, %r)\n'
                       'from gpu_counters_native import start_reference\nstart_reference(%r, %r, %r, %r, %r)\n' %
                       (str(marker), str(AGENT_DIR / 'devtools'), str(reference), str(observations),
                        args.event, args.probe_only, args.guard_resource), encoding='utf-8')
    config = Path(os.environ['APPDATA']) / 'qrendertest/UI.config'
    configs = {path: digest(path) for path in (config, config.with_name('DefaultLayout.config'))}
    environment = dict(os.environ, RENDERTEST_AGENT_ROOT=str(root / 'ipc'))
    environment.pop('RENDERTEST_AGENT_CHAT_AUTOTEST', None)
    environment.pop('RENDERTEST_AGENT_BRIDGE', None)
    info = subprocess.STARTUPINFO()
    info.dwFlags, info.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 0
    report = {'passed': False, 'root': str(root), 'capture': str(args.launch), 'exe': str(args.exe),
              'guardEventId': args.event, 'guardResourceFilter': args.guard_resource,
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
        require(instance is not None, 'Capture/native duration probe timed out.')
        report.update(instance=instance, configWriterClosed=True)
        native = json.loads(reference.read_text(encoding='utf-8'))
        report['nativeReference'] = native
        require('error' not in native, str(native.get('error')))
        require(native['guardStabilityVerified'], 'Guard did not pass the no-counter replay stability check.')
        event = native['guardEventId']
        report['guardEventId'] = event
        if args.probe_only:
            for key in ('fatalBefore', 'fatalAfterFetch', 'fatalAfterForcedRestore'):
                require(native[key]['code'] == 0, 'Native replay failed: ' + native[key]['message'])
            require(native['rows'] and any(row['valid'] for row in native['rows']), 'No valid GPU durations.')
            require(native['pixelsRestored'], 'Forced replay did not restore actual resource state.')
            print('Native duration:', json.dumps({'counterCount': len(native['counters']),
                'rows': len(native['rows']), 'validRows': sum(row['valid'] for row in native['rows']),
                'elapsedMs': native['elapsedMs'], 'description': native['durationDescription'],
                'pixelsRestored': native['pixelsRestored'], 'stateChangedByFetch': native['stateChangedByFetch']},
                ensure_ascii=True), flush=True)
        else:
            require(native['sameMeasurementObserverInstalled'], 'Native observer not installed.')
            check = Check(root / 'ipc', instance, observations)
            check.call('set_event', instance_id=check.instance, event_id=event)
            before = check.ui_event()
            require(before['currentEventId'] == event, 'Owned test GUI did not select guard event.')
            report['uiEventBefore'] = before
            report['catalog'] = check.catalog(native)
            report['duration'] = check.duration(native, event)
            report['uiEventAfter'] = check.ui_event()
            require(report['uiEventAfter'] == before, 'Counter tools changed the selected UI event.')
            report['uiEventPreserved'] = True
            print('Duration exact same-sample values, paging, scopes, refresh and state restoration passed.', flush=True)
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
