"""Validate bounded shader-reference scans in an owned hidden RenderTest process.

Examples (no model or external service is used):
  python agent/devtools/shader_references_live.py --launch E:/Captures/pragmata1.rdc --parent-event 3738 --event 3820 --guard-resource 390180 --guard-point 340 576
  python agent/devtools/shader_references_live.py --launch E:/Captures/恋与深空尝试截帧.rdc --parent-event 2712 --event 27 --guard-resource 72716

The independent native reference replays at most 64 candidate actions in the explicit
subtree, never the entire frame. Configuration writing is disabled before capture load;
only this runner's Popen is closed. --probe-only does not require the new tool deployed.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from decompile_live import AGENT_DIR, REPO_DIR, Check as BaseCheck, digest, error_text, require
from rendertest_mcp.ipc_client import BridgeClient


STAGES = ('vertex', 'hull', 'domain', 'geometry', 'pixel', 'compute', 'amplification', 'mesh')


class Check(BaseCheck):
    def __init__(self, root, instance, observation_path, reference):
        super().__init__(root, instance)
        self.observation_path, self.reference = observation_path, reference

    def observations(self):
        return json.loads(self.observation_path.read_text(encoding='utf-8'))['batches']

    def counters(self):
        batches = self.observations()
        return {'batches': len(batches), 'moves': sum(len(item['frameEvents']) for item in batches),
                'shaderReads': sum(len(item['shaderReads']) for item in batches)}

    def expected(self, resource, stages, parent=None):
        selected = set(stages or STAGES)
        scope = self.reference['scope']
        if parent is not None:
            nodes = {item['eventId']: item for item in scope}
            ids, pending = set(), [parent]
            while pending:
                eid = pending.pop()
                ids.add(eid)
                pending.extend(nodes[eid]['children'])
        else:
            ids = {item['eventId'] for item in scope}
        candidates = [item for item in scope if item['eventId'] in ids and selected.intersection(item['stages'])]
        rows = []
        for row in self.reference['bindings']:
            if row['eventId'] not in ids:
                continue
            matches = [item for item in row['bindings'] if item['resourceId'] == resource and item['stage'] in selected]
            if matches:
                rows.append({'eventId': row['eventId'], 'parentEventId': row['parentEventId'],
                             'name': row['name'], 'matches': matches})
        return rows, candidates, ids

    def collect(self, resource, stages=None, limit=1, scan_budget=1, refresh=False, parent=None):
        parent = self.reference['parentEventId'] if parent is None else parent
        expected, candidates, ids = self.expected(resource, stages, parent)
        pages, rows, cursor, processed = [], [], None, 0
        counters_before = self.counters()
        scan, started = None, None
        for page_number in range(128):
            response = self.call('find_shader_references', resource_id=resource, stages=stages,
                                 parent_event_id=parent, cursor=cursor, limit=limit,
                                 scan_budget=scan_budget, refresh=refresh and page_number == 0)
            require(response['effect'] == 'read', 'Reference search must have read effect.')
            data, coverage = response['data'], response['data']['coverage']
            if scan is None:
                scan, started = data['scanId'], data['scanStartedAtUtc']
            require(data['scanId'] == scan and data['scanStartedAtUtc'] == started,
                    'Pagination mixed scan generations.')
            require(data['resourceId'] == resource and data['parentEventId'] == parent,
                    'Reference search changed target/scope.')
            require(data['stages'] == [stage for stage in STAGES if stage in (stages or STAGES)],
                    'Stage canonicalization or duplicate removal differs.')
            require(coverage['scopeActionCount'] == len(ids) and
                    coverage['candidateEventCount'] == len(candidates), 'Independent candidate coverage differs.')
            examined = coverage['examinedThisPage']
            require(0 <= examined <= scan_budget and examined == coverage['nativeReadsThisPage'] +
                    coverage['cacheHitsThisPage'], 'Page scan budget/cache accounting differs.')
            processed += examined
            require(coverage['processedCandidateCount'] == processed, 'Processed prefix does not advance correctly.')
            require(len(data['references']) <= limit, 'Reference match limit exceeded.')
            rows.extend(data['references'])
            pages.append(response)
            complete = processed == len(candidates)
            require(coverage['scanComplete'] == complete and coverage['complete'] == complete,
                    'Bounded supported scope completeness is incorrect.')
            require(coverage['unsupportedActionCount'] == 0 and not coverage['unsupportedStages'],
                    'Chosen native fixture unexpectedly has unsupported coverage.')
            if response['nextCursor'] is None:
                require(complete, 'Search stopped before scanning all candidates.')
                break
            require(not complete and response['nextCursor'] != cursor, 'Nonadvancing or unnecessary cursor.')
            cursor = response['nextCursor']
        else:
            raise RuntimeError('Bounded pagination did not finish.')
        require([row['eventId'] for row in rows] == [row['eventId'] for row in expected],
                'Shader references differ from independent native GetShader readings.')
        for actual, native in zip(rows, expected):
            require(actual['parentEventId'] == native['parentEventId'] and actual['name'] == native['name'],
                    'Reference action identity differs.')
            require(actual['matches'] == native['matches'], 'Native Shader ResourceId/entry point/stage differs.')
        counters_after = self.counters()
        return {'resourceId': resource, 'stages': stages, 'parentEventId': parent,
                'scanId': scan, 'pages': pages, 'references': rows,
                'nativeReads': sum(item['data']['coverage']['nativeReadsThisPage'] for item in pages),
                'cacheHits': sum(item['data']['coverage']['cacheHitsThisPage'] for item in pages),
                'observerBefore': counters_before, 'observerAfter': counters_after,
                'nativeReferenceMatched': True}

    def rejected(self, code, **params):
        before = self.counters()
        try:
            self.call('find_shader_references', **params)
        except Exception as error:
            message = error_text(error)
            require(code in message, 'Unexpected reference rejection: ' + message)
            require(self.counters() == before, 'Rejected arguments caused native replay.')
            return {'params': params, 'error': message}
        raise RuntimeError('Invalid shader reference request was accepted.')

    def verify_restoration(self):
        # get_context also reads currently bound shaders without replaying. Only scan
        # callbacks move the replay event and require a restoration measurement.
        batches = [item for item in self.observations() if item['shaderReads'] and item['frameEvents']]
        require(batches, 'No native binding scan was observed.')
        candidate_ids = {item['eventId'] for item in self.reference['bindings']}
        visited, changed = Counter(), 0
        for batch in batches:
            for key in ('fatalBefore', 'fatalAfter'):
                require(batch[key]['code'] == 0, 'Native replay failed: ' + batch[key]['message'])
            require(batch['pixelsBefore'] == self.reference['pixelsBefore'] and
                    batch['pixelsAfter'] == batch['pixelsBefore'], 'Scan did not restore bit-exact guard resource state.')
            require(batch['frameEvents'][-1] == {'eventId': self.reference['guardEventId'], 'force': True},
                    'Native scan did not end with forced replay to the UI event.')
            require(batch['pixelsBeforeRestore'] is not None, 'Pre-restore resource state was not observed.')
            changed += int(batch['pixelsBeforeRestore'] != batch['pixelsBefore'])
            for read in batch['shaderReads']:
                require(read['eventId'] in candidate_ids, 'Production scanner replayed outside the chosen subtree.')
                visited[(read['eventId'], read['stage'])] += 1
                native = next(item for item in self.reference['bindings'] if item['eventId'] == read['eventId'])
                bound = next((item['resourceId'] for item in native['bindings'] if item['stage'] == read['stage']), None)
                require(read['resourceId'] == bound, 'Observed native scan differs from independent reference.')
        require(changed > 0, 'Guard never observed a scan-induced resource-state change.')
        expected_reads = Counter({(item['eventId'], stage): 2 for item in self.reference['scope']
                                  for stage in item['stages']})
        require(visited == expected_reads, 'Native stage reads were not exactly once per event per scan generation.')
        return {'batches': len(batches), 'scanChangedResourceStateBatches': changed,
                'guardPixelCount': len(self.reference['pixelsBefore']), 'forcedBitExactRestore': True,
                'visitedEventStageCounts': [{'eventId': eid, 'stage': stage, 'reads': count}
                                            for (eid, stage), count in sorted(visited.items())]}

    def run(self):
        frequencies = Counter((item['resourceId'], item['stage']) for row in self.reference['bindings']
                              for item in row['bindings'])
        require(frequencies, 'Bounded reference has no shaders.')
        choices = sorted(frequencies, key=lambda key: (-frequencies[key], key[1] != 'pixel', key))
        target, stage = choices[0]
        require(frequencies[(target, stage)] >= 2, 'Fixture needs repeated references to validate match pagination.')
        another = next((item for item in choices if item[0] != target), None)
        require(another is not None, 'Fixture needs another shader to validate cross-resource cache reuse.')
        bound_ids = {key[0] for key in choices}
        absent = next((item['resourceId'] for item in self.reference['shaderResources']
                       if item['resourceId'] not in bound_ids), None)
        require(absent is not None, 'Fixture needs a known shader absent from the scope.')
        result = {'targetResourceId': target, 'targetStage': stage}
        first = result['initial'] = self.collect(target, [stage, stage])
        require(first['nativeReads'] == len(self.reference['bindings']) and first['cacheHits'] == 0,
                'Initial search did not scan each candidate exactly once.')
        require(len(first['pages']) >= 2, 'Initial fixture did not exercise pagination.')
        old_cursor = first['pages'][0]['nextCursor']
        for name, resource, stages in (('otherShader', another[0], [another[1]]),
                                       ('allStages', target, None), ('emptyReferences', absent, [stage])):
            item = result[name] = self.collect(resource, stages)
            require(item['nativeReads'] == 0 and item['observerBefore'] == item['observerAfter'],
                    'A cache-only shader/stage query replayed native state.')
            require(item['scanId'] == first['scanId'], 'Cache reuse changed scan generation.')
        empty = result['emptyReferences']['pages'][0]
        require(not empty['data']['references'] and empty['nextCursor'] and
                not empty['data']['coverage']['complete'], 'Empty partial page was mistaken for a complete search.')
        result['stageExcluded'] = self.collect(target, ['compute'])
        require(result['stageExcluded']['observerBefore'] == result['stageExcluded']['observerAfter'] and
                not result['stageExcluded']['references'], 'Stage-excluded actions were replayed or matched stale shaders.')
        result['singleAction'] = self.collect(target, [stage], parent=first['references'][0]['eventId'])
        require(result['singleAction']['nativeReads'] == 0 and len(result['singleAction']['references']) == 1,
                'Exact leaf scope did not reuse cache.')
        parent = self.reference['parentEventId']
        result['rejections'] = [self.rejected('INVALID_RESOURCE', resource_id='18446744073709551615',
                                             parent_event_id=parent),
                                self.rejected('INVALID_RESOURCE', resource_id=self.reference['guardResourceId'],
                                              parent_event_id=parent),
                                self.rejected('INVALID_EVENT', resource_id=target, parent_event_id=0)]
        refreshed = result['refreshed'] = self.collect(target, [stage], refresh=True)
        require(refreshed['scanId'] != first['scanId'] and refreshed['nativeReads'] == first['nativeReads'],
                'Explicit refresh did not invalidate/rebuild the shared native cache.')
        result['rejections'].append(self.rejected('INVALID_REQUEST', resource_id=target, stages=[stage, stage],
                                                 parent_event_id=parent, cursor=old_cursor, limit=1, scan_budget=1))
        result['restoration'] = self.verify_restoration()
        return result


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--launch', type=Path, required=True)
    parser.add_argument('--exe', type=Path, default=REPO_DIR / 'x64' / 'Release' / 'qrendertest.exe')
    parser.add_argument('--parent-event', type=int, required=True)
    parser.add_argument('--event', type=int, required=True, help='Owned UI event used for actual-state restoration.')
    parser.add_argument('--guard-resource', required=True)
    parser.add_argument('--guard-grid', type=int, default=3, help='2..17 evenly spaced guard points per axis.')
    parser.add_argument('--guard-point', type=int, nargs=2, action='append', metavar=('X', 'Y'),
                        help='Fixed texture coordinates instead of a grid; repeat up to 16 times.')
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--wait', type=float, default=240)
    args = parser.parse_args()
    if os.name != 'nt' or min(args.event, args.parent_event) < 1 or not 0 < args.wait <= 1800:
        parser.error('Windows, positive EIDs and wait in (0,1800] are required.')
    if not args.guard_resource.isdecimal() or int(args.guard_resource) <= 0:
        parser.error('--guard-resource must be a positive decimal ResourceId.')
    if not 2 <= args.guard_grid <= 17:
        parser.error('--guard-grid must be 2..17.')
    if args.guard_point and (len(args.guard_point) > 16 or args.guard_grid != 3 or
                            any(min(point) < 0 for point in args.guard_point)):
        parser.error('--guard-point needs at most 16 nonnegative coordinates and cannot be combined with a custom grid.')
    for name in ('launch', 'exe'):
        value = getattr(args, name).resolve()
        if not value.is_file():
            parser.error('--%s is not an existing file: %s' % (name, value))
        setattr(args, name, value)
    return args


def main():
    args = arguments()
    root = AGENT_DIR / '.agent-data' / ('shader-references-live-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    reference_path, observation_path = root / 'native-reference.json', root / 'native-observations.json'
    config = Path(os.environ['APPDATA']) / 'qrendertest' / 'UI.config'
    configs = {path: digest(path) for path in (config, config.with_name('DefaultLayout.config'))}
    marker, startup = root / 'config-closed.txt', root / 'startup.py'
    startup.write_text("import pathlib, sys\npyrenderdoc.Config().Close()\n"
                       "pathlib.Path(%r).write_text('closed')\n" % str(marker) +
                       "sys.path.insert(0, %r)\n" % str(AGENT_DIR / 'devtools') +
                       "from shader_references_native import start_reference\n"
                       "start_reference(%r, %r, %r, %r, %r, %r, %r, %r)\n" %
                       (str(reference_path), str(observation_path), args.parent_event, args.event,
                        args.guard_resource, args.probe_only, args.guard_grid, args.guard_point), encoding='utf-8')
    environment = dict(os.environ, RENDERTEST_AGENT_ROOT=str(root / 'ipc'))
    environment.pop('RENDERTEST_AGENT_CHAT_AUTOTEST', None)
    environment.pop('RENDERTEST_AGENT_BRIDGE', None)
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags, startup_info.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 0
    report = {'passed': False, 'root': str(root), 'capture': str(args.launch), 'exe': str(args.exe),
              'parentEventId': args.parent_event, 'eventId': args.event,
              'guardResourceId': args.guard_resource, 'guardGrid': args.guard_grid, 'guardPoints': args.guard_point,
              'probeOnly': args.probe_only, 'errors': []}
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
        for key in ('fatalBefore', 'fatalAfter'):
            require(reference[key]['code'] == 0, 'Native reference device error: ' + reference[key]['message'])
        require(reference['pixelsBefore'] == reference['pixelsAfterRepeatedForce'] == reference['pixelsAfterRestore'],
                'Independent reference did not establish stable/restored resource state.')
        print('Native bounded reference:', len(reference['bindings']), 'candidates,',
              round(reference['elapsedMs'], 1), 'ms', flush=True)
        if not args.probe_only:
            check = Check(root / 'ipc', instance, observation_path, reference)
            check.call('set_event', instance_id=check.instance, event_id=args.event)
            report['uiEventBefore'] = check.ui_event()
            require(report['uiEventBefore']['currentEventId'] == args.event, 'Owned guard UI event was not selected.')
            report['validation'] = check.run()
            report['uiEventAfter'] = check.ui_event()
            require(report['uiEventAfter'] == report['uiEventBefore'], 'Reference search changed UI selection.')
            report['nativeObservations'] = str(observation_path)
            print('Native bindings, cache paging/refresh and bit-exact resource restoration passed.', flush=True)
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
