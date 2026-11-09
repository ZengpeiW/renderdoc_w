"""Smoke-test a relocated RenderTest trial package without contacting an AI provider.

Usage (the outer interpreter needs only Python 3.10+ standard library):
  python agent/devtools/trial_package_smoke.py --package PATH_OR_ZIP --capture E:/Captures/pragmata1.rdc --parent-event 7726

Expected layout: qrendertest.exe, runtime/python/python.exe, and
integrations/rendertest_agent/{python/rendertest_agent_bridge,skills/<four skills>}.
The package is copied/extracted into a fresh path containing spaces. Its own isolated Python
runs the checks, including a real stdio MCP server and one bounded get_pass_resources call.
The test closes only its own GUI process. No harness is started and no credentials are read.
Temporary profile variables isolate Python/agent data; Windows Qt known-folder configuration
is additionally protected with Config.Close() before capture loading and before/after hashes.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import zipfile


SKILLS = ('rendertest-core', 'rendertest-event-analysis', 'rendertest-pixel-investigation', 'rendertest-shader-debug')
MAX_FILES = 50000
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    if not path.is_file():
        return None
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding='utf-8')
    temporary.replace(path)


def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def _archive_parts(name):
    name = name.replace('\\', '/')
    path = PurePosixPath(name)
    require(not path.is_absolute() and path.parts, 'Archive contains an absolute/empty path.')
    for part in path.parts:
        require(part not in ('.', '..') and ':' not in part and part == part.rstrip(' .'),
                'Archive contains an unsafe path component.')
        require(not re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part),
                'Archive contains a reserved Windows path.')
    return path.parts


def relocate(source, destination):
    require(not destination.exists(), 'Relocation target must be new.')
    require(not source.is_dir() or not inside(destination, source), 'Relocation destination cannot be inside the source package.')
    destination.mkdir(parents=True)
    files, total = 0, 0
    if source.is_file():
        require(zipfile.is_zipfile(source), '--package file must be a ZIP archive.')
        with zipfile.ZipFile(source) as archive:
            seen = set()
            entries = archive.infolist()
            require(len(entries) <= MAX_FILES, 'Archive has too many entries.')
            for entry in entries:
                parts = _archive_parts(entry.filename)
                key = '/'.join(parts).casefold()
                require(key not in seen, 'Archive contains duplicate Windows paths.')
                seen.add(key)
                mode = entry.external_attr >> 16
                require(not stat.S_ISLNK(mode) and not (entry.external_attr & 0x400),
                        'Archive contains a symlink/reparse point.')
                require(not entry.flag_bits & 1, 'Encrypted archive entries are unsupported.')
                target = destination.joinpath(*parts)
                require(inside(target, destination), 'Archive target escapes relocation directory.')
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                files += 1
                total += entry.file_size
                require(entry.file_size <= MAX_FILE_BYTES and total <= MAX_TOTAL_BYTES,
                        'Archive exceeds the extraction size budget.')
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                require(target.stat().st_size == entry.file_size, 'Extracted file size differs from ZIP metadata.')
    else:
        require(source.is_dir(), '--package is not an existing directory or ZIP.')
        require(not (source.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT),
                'Source package root is a reparse point.')
        for base, directories, names in os.walk(source, followlinks=False):
            for name in directories + names:
                original = Path(base) / name
                metadata = original.lstat()
                require(not stat.S_ISLNK(metadata.st_mode) and
                        not (metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT),
                        'Source package contains a symlink/reparse point.')
                target = destination / original.relative_to(source)
                require(inside(target, destination), 'Copy target escapes relocation directory.')
                if original.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    files += 1
                    total += metadata.st_size
                    require(files <= MAX_FILES and metadata.st_size <= MAX_FILE_BYTES and total <= MAX_TOTAL_BYTES,
                            'Package exceeds the copy size budget.')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, target)
    candidates = [destination] if (destination / 'qrendertest.exe').is_file() else [
        child for child in destination.iterdir() if child.is_dir() and (child / 'qrendertest.exe').is_file()]
    require(len(candidates) == 1, 'Package must contain exactly one top-level RenderTest application directory.')
    return candidates[0], {'fileCount': files, 'unpackedBytes': total}


def user_config_paths():
    # Query Windows known-folder location without reading any credential/config contents.
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x001A, None, 0, buffer)
    require(result == 0 and buffer.value, 'Could not identify the user Qt configuration directory.')
    folder = Path(buffer.value) / 'qrendertest'
    return [folder / 'UI.config', folder / 'DefaultLayout.config']


def isolated_environment(package, work):
    # Build an allowlist instead of copying credentials from the caller's environment.
    names = ('SystemRoot', 'WINDIR', 'SystemDrive', 'COMSPEC', 'PATHEXT', 'OS',
             'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER')
    env = {name: os.environ[name] for name in names if name in os.environ}
    windows = Path(env.get('SystemRoot', 'C:/Windows'))
    profile = work / 'Empty User Profile'
    folders = {'USERPROFILE': profile, 'HOME': profile, 'APPDATA': profile / 'AppData/Roaming',
               'LOCALAPPDATA': profile / 'AppData/Local', 'TEMP': work / 'Temp', 'TMP': work / 'Temp',
               'CODEX_HOME': profile / '.codex', 'CLAUDE_CONFIG_DIR': profile / '.claude'}
    for name, path in folders.items():
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    # Analytics::Prompt runs before --python. Seed only this disposable profile so
    # a first launch cannot wait on its analytics dialog or check for updates.
    temporary_config = folders['APPDATA'] / 'qrendertest/UI.config'
    temporary_config.parent.mkdir(parents=True, exist_ok=True)
    temporary_config.write_text(json.dumps({'rdocConfigData': 1, 'Analytics_TotalOptOut': True,
                                            'CheckUpdate_AllowChecks': False}), encoding='utf-8')
    env['PATH'] = os.pathsep.join(str(path) for path in (package, package / 'runtime/python',
                                                       windows / 'System32', windows, windows / 'System32/Wbem'))
    env['RENDERTEST_AGENT_ROOT'] = str(work / 'Private Agent Data')
    env['RENDERTEST_MCP_PYTHON'] = str(package / 'runtime/python/python.exe')
    env['RENDERTEST_AGENT_BRIDGE'] = '1'
    env['PYTHONNOUSERSITE'] = '1'
    return env


def validate_python(package):
    python_root = (package / 'runtime/python').resolve()
    require(Path(sys.executable).resolve() == python_root / 'python.exe', 'Worker is not using the package Python.')
    require(sys.flags.isolated and sys.flags.no_user_site, 'Package worker is not running in isolated mode.')
    require(list(python_root.glob('python*._pth')), 'Embeddable Python _pth isolation file is missing.')
    require(all(entry and inside(entry, python_root) for entry in sys.path), 'Python search path escapes its bundled runtime.')
    modules = {}
    for name in ('rendertest_mcp', 'rendertest_mcp.server', 'rendertest_mcp.claude_backend',
                 'claude_agent_sdk', 'mcp', 'anyio', 'pydantic', 'pydantic_core'):
        module = importlib.import_module(name)
        origin = getattr(module, '__file__', None)
        require(origin and inside(origin, python_root), 'Dependency resolved outside the package: ' + name)
        modules[name] = str(Path(origin).resolve())
    for name, module in list(sys.modules.items()):
        origin = getattr(module, '__file__', None)
        if origin and not origin.startswith('<') and module is not sys.modules.get('__main__'):
            require(inside(origin, python_root), 'An imported module escaped bundled Python: ' + name)
    return {'executable': sys.executable, 'version': sys.version, 'sysPath': sys.path,
            'isolated': True, 'modules': modules,
            'versions': {name: importlib.metadata.version(name) for name in
                         ('rendertest-agent-mcp', 'mcp', 'anyio', 'pydantic', 'claude-agent-sdk')},
            'claudeBackendCheck': 'import only; no SDK client, CLI, account or network operation'}


def validate_skills(package):
    root = package / 'integrations/rendertest_agent/skills'
    require(root.is_dir(), 'Deployed skill directory is missing.')
    names = sorted(path.parent.name for path in root.glob('*/SKILL.md'))
    require(names == sorted(SKILLS), 'Expected exactly the four RenderTest skills.')
    results = []
    for name in SKILLS:
        path = root / name / 'SKILL.md'
        source = path.read_text(encoding='utf-8')
        require(source.startswith('---') and re.search(r'^name:\s*' + re.escape(name) + r'\s*$', source, re.M),
                'Skill front matter has an unexpected name: ' + name)
        require(len(source) > 200, 'Deployed skill appears empty: ' + name)
        results.append({'name': name, 'path': str(path), 'sha256': digest(path)})
    return results


def structured(value):
    require(not value.isError, 'MCP tool returned an error.')
    data = getattr(value, 'structuredContent', None)
    if isinstance(data, dict) and 'schema' in data:
        return data
    for content in value.content:
        if getattr(content, 'type', None) == 'text':
            parsed = json.loads(content.text)
            if isinstance(parsed, dict) and 'schema' in parsed:
                return parsed
    raise RuntimeError('MCP tool did not return the expected structured result.')


async def check_mcp(package, work, instance, parent_event):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from rendertest_mcp.protocol import RESULT_SCHEMA, TOOL_NAMES
    env = dict(os.environ)
    env.update(RENDERTEST_AGENT_FIXED_INSTANCE_ID=instance['instanceId'],
               RENDERTEST_AGENT_INSTANCE_ID=instance['instanceId'],
               RENDERTEST_AGENT_CAPTURE_SESSION_ID=instance['captureSessionId'])
    parameters = StdioServerParameters(command=str(package / 'runtime/python/python.exe'),
                                       args=['-I', '-m', 'rendertest_mcp'], env=env,
                                       cwd=str(package / 'integrations/rendertest_agent'))
    with (work / 'mcp.stderr.log').open('w', encoding='utf-8') as errors:
        async with stdio_client(parameters, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                names = sorted(tool.name for tool in listed.tools)
                require(len(names) == 35 and set(names) == set(TOOL_NAMES), 'The package does not expose exactly 35 expected tools.')
                require({'get_pass_resources', 'find_shader_references', 'debug_compute'} <= set(names),
                        'New analysis tools are missing from the package.')
                async def call(name, params):
                    value = structured(await session.call_tool(name, params))
                    require(value['schema'] == RESULT_SCHEMA and value['instanceId'] == instance['instanceId'] and
                            value['captureSessionId'] == instance['captureSessionId'], 'MCP result escaped the fixed capture scope.')
                    return value
                before = await call('get_context', {})
                result = await call('get_pass_resources', {'parent_event_id': parent_event, 'limit': 3, 'scan_budget': 200})
                after = await call('get_context', {})
                for key in ('currentEventId', 'selectedEventId'):
                    require(before['data'][key] == after['data'][key], 'Read-only package smoke changed the UI event.')
                coverage = result['data']['coverage']
                require(result['effect'] == 'read' and result['data']['scope']['parentEventId'] == parent_event,
                        'Pass-resource result has the wrong effect/scope.')
                require(0 < coverage['examinedThisPage'] <= 200 and coverage['nativeReadsThisPage'] > 0 and
                        len(result['data']['resources']) <= 3, 'Bounded native usage query did not execute.')
                return {'toolNames': names, 'toolCount': len(names),
                        'serverInfo': initialized.serverInfo.model_dump(), 'passResources': result,
                        'uiBefore': {key: before['data'][key] for key in ('currentEventId', 'selectedEventId')},
                        'uiAfter': {key: after['data'][key] for key in ('currentEventId', 'selectedEventId')},
                        'passResourcesCalls': 1, 'onlineModelCalls': 0}


def worker(args):
    package, work = args.package.resolve(), args.work_dir.resolve()
    report = {'passed': False, 'package': str(package), 'capture': str(args.capture), 'errors': [],
              'isolationBoundary': 'Bundled Python, credentials and agent data isolated. Qt Windows known-folder '
                                   'configuration is protected by Config.Close and external hash checks, not a new Windows account.'}
    process = None
    try:
        report['python'] = validate_python(package)
        report['skills'] = validate_skills(package)
        native_python_files = list((package / 'integrations/rendertest_agent/python').rglob('*.py'))
        require(native_python_files, 'Native Bridge Python files are missing.')
        report['longestNativePythonPath'] = max(len(str(path)) for path in native_python_files)
        require(report['longestNativePythonPath'] < 260,
                'Native Python 3.6 needs a shorter extraction path; choose a short --work-dir (e.g. E:/Captures/TrialValidation).')
        package_check = package / 'tools/check_environment.py'
        require(package_check.is_file(), 'Package is missing its offline environment checker.')
        checked = subprocess.run([sys.executable, '-I', str(package_check), '--verify-files'],
                                 cwd=str(package), env=dict(os.environ), capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=120)
        report['offlinePackageCheck'] = {'returnCode': checked.returncode, 'stdout': checked.stdout,
                                        'stderr': checked.stderr, 'verifiedManifestFiles': checked.returncode == 0}
        require(checked.returncode == 0, 'Packaged offline dependency/file-hash check failed.')
        from rendertest_mcp.ipc_client import BridgeClient
        native_report = work / 'native-startup.json'
        startup = work / 'native-startup.py'
        startup.write_text("pyrenderdoc.Config().Close()\n"
                           "import json, pathlib, sys, traceback\n"
                           "try:\n"
                           "    import qrenderdoc, renderdoc\n"
                           "    import rendertest_agent_bridge as bridge\n"
                           "    data = {'configClosed': True, 'configPath': str(qrenderdoc.ConfigFilePath('UI.config')), "
                           "'bridgeModule': bridge.__file__, 'renderdocModule': getattr(renderdoc, '__file__', None), "
                           "'qrenderdocModule': getattr(qrenderdoc, '__file__', None), "
                           "'renderdocOrigin': getattr(renderdoc.__spec__, 'origin', None), "
                           "'qrenderdocOrigin': getattr(qrenderdoc.__spec__, 'origin', None), "
                           "'executable': sys.executable, 'sysPath': sys.path}\n"
                           "except BaseException:\n"
                           "    data = {'configClosed': True, 'error': traceback.format_exc()}\n"
                           "pathlib.Path(%r).write_text(json.dumps(data), encoding='utf-8')\n" % str(native_report), encoding='utf-8')
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags, startup_info.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 0
        with (work / 'gui.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(package / 'qrendertest.exe'), '--python', str(startup), str(args.capture)],
                                       cwd=str(package), env=dict(os.environ), startupinfo=startup_info,
                                       stdout=log, stderr=subprocess.STDOUT)
            report['ownedPid'] = process.pid
            write_json(work / 'owned-gui.json', {'pid': process.pid, 'executable': str(package / 'qrendertest.exe')})
            discovery = BridgeClient(root=Path(os.environ['RENDERTEST_AGENT_ROOT']), environ={})
            deadline, instance = time.monotonic() + args.wait, None
            while time.monotonic() < deadline:
                require(process.poll() is None, 'Packaged GUI exited during capture startup.')
                if native_report.is_file():
                    startup_result = json.loads(native_report.read_text(encoding='utf-8'))
                    require('error' not in startup_result, startup_result.get('error', ''))
                candidates = [item for item in discovery.list_instances()
                              if item.get('pid') == process.pid and item.get('captureLoaded')]
                if len(candidates) == 1 and native_report.is_file():
                    instance = candidates[0]
                    break
                time.sleep(.25)
            require(instance is not None, 'Packaged GUI/Bridge did not load the capture before timeout.')
            report['instance'] = instance
            native = json.loads(native_report.read_text(encoding='utf-8'))
            require(native['configClosed'], 'Native configuration writer was not closed.')
            require(inside(native['bridgeModule'], package), 'Bridge module loaded outside the relocated package.')
            for name in ('renderdoc', 'qrenderdoc'):
                origin = native[name + 'Module']
                require(inside(origin, package) if origin else native[name + 'Origin'] == 'built-in',
                        'Native binding is not builtin or packaged: ' + name)
            report['native'] = native
            report['mcp'] = asyncio.run(asyncio.wait_for(check_mcp(package, work, instance, args.parent_event), timeout=90))
    except BaseException as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    finally:
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
                report['errors'].append('Owned GUI cleanup: ' + str(error))
        report['passed'] = not report['errors'] and bool(report.get('ownedProcessExited'))
        write_json(work / 'worker-report.json', report)
    return 0 if report['passed'] else 1


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--parent-event', type=int, required=True)
    parser.add_argument('--work-dir', type=Path, help='Parent for a fresh retained smoke directory; never overwritten.')
    parser.add_argument('--wait', type=float, default=240)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt' or not 1 <= args.parent_event <= 0xffffffff or not 0 < args.wait <= 1800:
        parser.error('Windows, a positive uint32 parent EID, and wait in (0,1800] are required.')
    args.package, args.capture = args.package.resolve(), args.capture.resolve()
    if not args.package.exists() or not args.capture.is_file():
        parser.error('Package/capture paths must already exist.')
    return args


def main():
    args = arguments()
    if args.worker:
        return worker(args)
    base = args.work_dir.resolve() if args.work_dir else Path(__file__).resolve().parents[1] / '.agent-data'
    work = base / ('Trial Smoke ' + uuid.uuid4().hex[:12])
    work.mkdir(parents=True)
    report = {'passed': False, 'sourcePackage': str(args.package), 'workDir': str(work), 'errors': []}
    configs = {path: digest(path) for path in user_config_paths()}
    try:
        package, copied = relocate(args.package, work / 'Relocated App')
        report['package'], report['relocation'] = str(package), copied
        if args.package.is_file():
            report['archiveSha256'] = digest(args.package)
        python = package / 'runtime/python/python.exe'
        require(python.is_file(), 'Package is missing runtime/python/python.exe.')
        require(' ' in str(package), 'Relocation did not exercise a path containing spaces.')
        script = work / 'smoke worker.py'
        shutil.copy2(Path(__file__).resolve(), script)
        command = [str(python), '-I', str(script), '--worker', '--package', str(package),
                   '--capture', str(args.capture), '--parent-event', str(args.parent_event),
                   '--work-dir', str(work), '--wait', str(args.wait)]
        print('Relocated package:', package, 'Report:', work / 'report.json', flush=True)
        result = subprocess.run(command, cwd=str(work), env=isolated_environment(package, work),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8', errors='replace')
        (work / 'worker.stdout.log').write_text(result.stdout, encoding='utf-8')
        (work / 'worker.stderr.log').write_text(result.stderr, encoding='utf-8')
        report['workerReturnCode'] = result.returncode
        worker_path = work / 'worker-report.json'
        require(worker_path.is_file(), 'Bundled Python did not produce a worker report; inspect worker.stderr.log.')
        report['workerReport'] = str(worker_path)
        report['worker'] = json.loads(worker_path.read_text(encoding='utf-8'))
        require(result.returncode == 0 and report['worker']['passed'], 'Bundled package smoke failed; inspect worker-report.json.')
        actual_config = Path(report['worker']['native']['configPath']).resolve()
        require(actual_config in {path.resolve() for path in configs} or inside(actual_config, work),
                'Qt used an unmonitored configuration directory.')
    except BaseException as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    finally:
        report['configuration'] = {str(path): {'before': before, 'after': digest(path)} for path, before in configs.items()}
        report['configPreserved'] = all(item['before'] == item['after'] for item in report['configuration'].values())
        if not report['configPreserved']:
            report['errors'].append('User UI.config or DefaultLayout.config changed.')
        report['passed'] = not report['errors'] and bool(report.get('worker', {}).get('passed'))
        write_json(work / 'report.json', report)
        print('Report:', work / 'report.json', 'passed=' + str(report['passed']).lower(), flush=True)
        if report['errors']:
            print(json.dumps(report['errors'], ensure_ascii=True), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
