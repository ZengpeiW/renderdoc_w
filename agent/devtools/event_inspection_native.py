"""Independent native reference for event_inspection_live.py; run inside its owned GUI.

Only the structured API metadata is inspected. No replay event is moved here.
"""

import json
import math
import pathlib
import threading
import time
import traceback


def start_reference(output_path, event_id):
    import rendertest_agent_bridge as bridge

    def read(controller):
        structured = controller.GetStructuredFile()
        stack = list(controller.GetRootActions())
        target = None
        samples = {}
        while stack:
            action = stack.pop()
            stack.extend(action.children)
            if int(action.eventId) == event_id:
                target = action
            for event in action.events:
                if event.chunkIndex >= len(structured.chunks):
                    continue
                chunk = structured.chunks[event.chunkIndex]
                for kind in ('Clear', 'Copy', 'ResourceBarrier'):
                    if kind not in samples and kind in str(chunk.name):
                        samples[kind] = (int(action.eventId), event, chunk)

        if target is None:
            raise RuntimeError('Reference action does not exist: %s' % event_id)
        calls = []
        for event in target.events:
            available = event.chunkIndex < len(structured.chunks)
            chunk = structured.chunks[event.chunkIndex] if available else None
            calls.append({'apiEventId': int(event.eventId), 'chunkIndex': int(event.chunkIndex),
                          'name': str(chunk.name) if available else None,
                          'parameterCount': int(chunk.NumChildren()) if available else None,
                          'available': available})
            if available and int(event.eventId) == event_id:
                samples['Target'] = (event_id, event, chunk)

        def scalar(obj):
            base = str(obj.type.basetype).split('.')[-1]
            if base == 'GPUAddress':
                return '0x%x' % int(obj.data.basic.u)
            if base in ('UnsignedInteger', 'Enum', 'SignedInteger'):
                value = int(obj.data.basic.i if base == 'SignedInteger' else obj.data.basic.u)
                return value if abs(value) <= (1 << 53) - 1 else str(value)
            if base == 'Resource':
                value = int(obj.data.basic.u)
                return str(value) if value else None
            if base == 'Float':
                value = float(obj.data.basic.d)
                return value if math.isfinite(value) else ('NaN' if math.isnan(value) else
                                                           ('Infinity' if value > 0 else '-Infinity'))
            if base == 'Boolean':
                return bool(obj.data.basic.b)
            if base == 'String':
                return None if int(obj.type.flags) & 0x8 else str(obj.AsString())
            if base == 'Null':
                return None
            raise ValueError(base)

        references = []
        for kind, (owner, event, chunk) in sorted(samples.items()):
            leaves, arrays = [], []
            pending = [(chunk, '', 0)]
            visited = 0
            while pending and visited < 512 and len(leaves) < 32:
                obj, path, depth = pending.pop()
                visited += 1
                count = int(obj.NumChildren())
                if count:
                    if str(obj.type.basetype).endswith('.Array'):
                        arrays.append({'path': path, 'childCount': count})
                    if depth < 8:
                        for i in reversed(range(min(count, 16))):
                            pending.append((obj.GetChild(i), path + '[%d]' % i, depth + 1))
                else:
                    try:
                        value = scalar(obj)
                    except ValueError:
                        continue
                    if isinstance(value, str) and len(value) > 256:
                        continue
                    leaves.append({'path': path, 'name': str(obj.name),
                                   'baseType': str(obj.type.basetype).split('.')[-1],
                                   'value': value})
            references.append({'kind': kind, 'actionEventId': owner,
                               'apiEventId': int(event.eventId), 'name': str(chunk.name),
                               'chunkIndex': int(event.chunkIndex),
                               'leaves': leaves, 'arrays': arrays})
        return {'eventId': event_id, 'calls': calls, 'samples': references}

    def work():
        try:
            deadline = time.time() + 240
            while not bridge._runtime.state.snapshot().get('captureLoaded'):
                if time.time() > deadline:
                    raise RuntimeError('Native reference capture load timeout')
                time.sleep(.25)
            box = {}
            def invoke(controller):
                box['reference'] = read(controller)
            bridge._runtime.replay.manager.BlockInvoke(invoke)
            result = box['reference']
        except BaseException:
            result = {'error': traceback.format_exc()}
        path = pathlib.Path(output_path)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')
        temporary.replace(path)

    threading.Thread(target=work, daemon=True).start()
