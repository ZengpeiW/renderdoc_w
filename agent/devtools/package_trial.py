"""Build a relocatable Windows x64 RenderTest AI Assistant trial from verified binaries.

Run with the development MCP venv. No user config, account data, captures, build
symbols or editable venv paths are copied. AI provider CLIs remain user-installed.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile

from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parents[2]
PYTHON_FILE = 'python-3.14.7-embed-amd64.zip'
PYTHON_URL = 'https://www.python.org/ftp/python/3.14.7/' + PYTHON_FILE
PYTHON_SHA256 = 'd297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15'
NATIVE_FILES = (
    'qrendertest.exe', 'rendertest.dll', 'rendertestcmd.exe', 'rendertestshim64.dll',
    'rendertestui.exe', 'rendertest.json', 'python36.dll', 'python36.zip', '_ctypes.pyd',
    'Qt5Core.dll', 'Qt5Gui.dll', 'Qt5Network.dll', 'Qt5Svg.dll', 'Qt5Widgets.dll',
    'd3dcompiler_47.dll', 'dbghelp.dll', 'symsrv.dll', 'symsrv.yes',
)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def copy(source, destination):
    if not source.is_file() or source.is_symlink():
        raise RuntimeError('Missing or linked package input: ' + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8', newline='\n')


def dependencies():
    project = tomllib.loads((REPO / 'agent/mcp_server/pyproject.toml').read_text(encoding='utf-8'))['project']
    pending = [Requirement(line) for line in project['dependencies'] + project['optional-dependencies']['claude']]
    found, active_extras, visited = {}, {}, set()
    while pending:
        requested = pending.pop()
        dist = metadata.distribution(requested.name)
        if not requested.specifier.contains(dist.version):
            raise RuntimeError('Unsatisfied dependency: ' + str(requested))
        key = dist.metadata['Name'].lower().replace('_', '-')
        extras = active_extras.setdefault(key, set())
        extras.update(requested.extras)
        state = (key, tuple(sorted(extras)))
        if state in visited:
            continue
        visited.add(state)
        found[key] = dist
        for declaration in dist.requires or []:
            requirement = Requirement(declaration)
            if requirement.marker is None or any(requirement.marker.evaluate({'extra': extra}) for extra in {''} | extras):
                installed = metadata.distribution(requirement.name)
                if not requirement.specifier.contains(installed.version):
                    raise RuntimeError('Unsatisfied dependency: ' + declaration)
                pending.append(requirement)
    return found


def vendor_dependencies(destination):
    versions = {}
    for name, dist in sorted(dependencies().items()):
        versions[name] = dist.version
        origin = Path(dist.locate_file('')).resolve()
        for record in dist.files or []:
            relative = Path(str(record))
            if relative.is_absolute() or '..' in relative.parts:
                continue  # Installed console scripts are not used by the -m entrypoints.
            if '__pycache__' in relative.parts or relative.suffix.lower() in ('.pyc', '.lib', '.pdb', '.exp'):
                continue
            if '_bundled' in relative.parts or relative.name in ('direct_url.json', 'INSTALLER', 'REQUESTED'):
                continue
            if relative.name.startswith('_editable') or relative.suffix == '.exe':
                continue
            source = (origin / relative).resolve()
            if not source.is_relative_to(origin):
                raise RuntimeError('Dependency path escapes site-packages')
            if relative.suffix == '.pth' and str(REPO).lower() in source.read_text(encoding='utf-8').lower():
                raise RuntimeError('Developer checkout reference in .pth file')
            copy(source, destination / relative)
    # This project is installed editable in development; ship actual modules instead.
    source = REPO / 'agent/mcp_server/src/rendertest_mcp'
    for path in source.rglob('*.py'):
        copy(path, destination / 'rendertest_mcp' / path.relative_to(source))
    dist = destination / 'rendertest_agent_mcp-0.1.0.dist-info'
    text(dist / 'METADATA', 'Metadata-Version: 2.3\nName: rendertest-agent-mcp\nVersion: 0.1.0\nLicense: MIT\n')
    copy(REPO / 'LICENSE.md', dist / 'licenses/LICENSE.md')
    return versions


def inline(value):
    value = html.escape(value)
    value = re.sub(r'`([^`]+)`', r'<code>\1</code>', value)
    value = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', value)
    value = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'<a href="\2">\1</a>', value)
    return value


def render_guide(markdown):
    """Small offline renderer for the guide's headings, lists, tables and code blocks."""
    output, code, listing, table = [], False, None, False
    for line in markdown.splitlines():
        if line.startswith('```'):
            code = not code
            output.append('<pre><code>' if code else '</code></pre>')
            continue
        if code:
            output.append(html.escape(line) + '\n')
            continue
        kind = 'ol' if re.match(r'^\d+\. ', line) else 'ul' if line.startswith('- ') else None
        if listing and listing != kind:
            output.append('</' + listing + '>')
            listing = None
        is_table = line.startswith('|')
        if table and not is_table:
            output.append('</tbody></table></div>')
            table = False
        if not line.strip():
            continue
        if is_table:
            cells = line.strip('|').split('|')
            if all(re.fullmatch(r'[\s:-]+', cell) for cell in cells):
                continue
            if not table:
                output.append('<div class="table-wrap"><table><tbody>')
                table = True
            output.append('<tr>' + ''.join('<td>' + inline(cell.strip()) + '</td>' for cell in cells) + '</tr>')
        elif kind:
            if not listing:
                listing = kind
                output.append('<' + kind + '>')
            output.append('<li>' + inline(re.sub(r'^(?:- |\d+\. )', '', line)) + '</li>')
        elif line.startswith('#'):
            level = min(len(line) - len(line.lstrip('#')), 6)
            output.append('<h%d>%s</h%d>' % (level, inline(line[level:].strip()), level))
        elif line.startswith('> '):
            output.append('<blockquote>' + inline(line[2:]) + '</blockquote>')
        else:
            output.append('<p>' + inline(line) + '</p>')
    if listing:
        output.append('</' + listing + '>')
    if table:
        output.append('</tbody></table></div>')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RenderTest AI Assistant 使用指南</title>
<style>body{font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;color:#243043;background:#f5f7fa;margin:0}
main{max-width:1080px;margin:36px auto;padding:40px 52px;background:white;border-radius:14px;box-shadow:0 8px 40px #20305010}
h1{font-size:32px;line-height:1.35}h2{margin-top:48px;border-bottom:1px solid #dde4ed;padding-bottom:8px}h3{margin-top:30px}
code{background:#eff3f8;border-radius:4px;padding:2px 5px;font:14px/1.7 Consolas,monospace;overflow-wrap:anywhere}
pre{background:#152338;color:#e7edf5;padding:18px;overflow:auto;border-radius:8px}pre code{background:none;padding:0;color:inherit}
a{color:#1665a9}blockquote{margin-left:0;padding:12px 18px;background:#eef5ff;border-left:3px solid #397fbc}
.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td{border:1px solid #dce3ec;padding:10px;vertical-align:top}
tr:first-child{background:#edf3fa;font-weight:bold}li{margin:5px 0}@media(max-width:700px){main{margin:0;padding:24px 18px;border-radius:0}}
@media print{body{background:white}main{box-shadow:none;margin:0;padding:0}h2{break-after:avoid}tr,pre{break-inside:avoid}}</style>
<main>''' + '\n'.join(output) + '</main></html>\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='RenderTest-AI-Assistant-2026.11.09-win64')
    parser.add_argument('--python-zip', type=Path, default=REPO / 'package/_inputs' / PYTHON_FILE)
    parser.add_argument('--msvc-redist', type=Path, required=True)
    parser.add_argument('--no-zip', action='store_true', help='Stage only for relocation validation')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.name):
        parser.error('Package name must be a single ASCII directory name')
    if sys.version_info[:3] != (3, 14, 7) or os.name != 'nt':
        parser.error('Use the tested Windows Python 3.14.7 MCP venv')
    if sha(args.python_zip) != PYTHON_SHA256:
        raise RuntimeError('Python archive does not match the official release SHA-256')
    target = REPO / 'package' / args.name
    target.mkdir(parents=True, exist_ok=False)  # Never remove or overwrite a previous delivery.
    release = REPO / 'x64/Release'
    for name in NATIVE_FILES:
        copy(release / name, target / name)
    for folder in ('qtplugins', 'pymodules'):
        for path in (release / folder).rglob('*'):
            if path.is_file() and path.suffix in ('.dll', '.pyd'):
                copy(path, target / path.relative_to(release))
    redist_files = list(args.msvc_redist.glob('*.dll'))
    if not {'msvcp140.dll', 'vcruntime140.dll'}.issubset({p.name.lower() for p in redist_files}):
        raise RuntimeError('Microsoft app-local x64 CRT redist is incomplete')
    for path in redist_files:
        copy(path, target / path.name)
    integration = target / 'integrations/rendertest_agent'
    bridge = REPO / 'agent/bridge/rendertest_agent_bridge'
    for path in bridge.rglob('*'):
        if path.is_file() and path.suffix in ('.py', '.json'):
            copy(path, integration / 'python/rendertest_agent_bridge' / path.relative_to(bridge))
    for path in (REPO / '.agents/skills').glob('rendertest-*/SKILL.md'):
        for folder in ('skills', '.agents/skills'):
            copy(path, integration / folder / path.relative_to(REPO / '.agents/skills'))
    decompiler = release / 'integrations/rendertest_agent/tools/shader_decompiler'
    for path in decompiler.rglob('*'):
        if path.is_file():
            copy(path, integration / 'tools/shader_decompiler' / path.relative_to(decompiler))
    # Native decompilers may be started with a temporary cwd, including direct EXE launches.
    # Put the CRT beside each executable so they do not depend on the developer machine's PATH.
    for executable in (integration / 'tools/shader_decompiler').rglob('*.exe'):
        for path in redist_files:
            copy(path, executable.parent / path.name)
    python = target / 'runtime/python'
    python.mkdir(parents=True)
    with zipfile.ZipFile(args.python_zip) as archive:
        for info in archive.infolist():
            if not (python / info.filename).resolve().is_relative_to(python.resolve()):
                raise RuntimeError('Unsafe Python archive member')
        archive.extractall(python)
    text(python / 'python314._pth', 'python314.zip\n.\nLib/site-packages\nimport site\n')
    versions = vendor_dependencies(python / 'Lib/site-packages')
    copy(REPO / 'agent/distribution/check_environment.py', target / 'tools/check_environment.py')
    for name in ('Start-RenderTest.cmd', 'Check-Environment.cmd'):
        copy(REPO / 'agent/distribution' / name, target / name)
    guide = REPO / 'agent/docs/AI_ASSISTANT_USER_GUIDE.md'
    copy(guide, target / 'AI_ASSISTANT_USER_GUIDE.md')
    text(target / 'README.html', render_guide(guide.read_text(encoding='utf-8')))
    copy(REPO / 'LICENSE.md', target / 'LICENSE.md')
    copy(REPO / 'docs/credits_acknowledgements.rst', target / 'docs/credits_acknowledgements.rst')
    copy(REPO / 'qrenderdoc/3rdparty/qt/ThirdPartySoftware_Listing.txt', target / 'licenses/native/qt/ThirdPartySoftware_Listing.txt')
    for root in ('qrenderdoc/3rdparty', 'renderdoc/3rdparty'):
        for path in (REPO / root).rglob('*'):
            if path.is_file() and any(word in path.name.lower() for word in ('license', 'copying', 'notice')):
                if path.stat().st_size < 2 * 1024 * 1024 and path.suffix.lower() not in ('.exe', '.dll', '.pdb'):
                    copy(path, target / 'licenses/native' / path.relative_to(REPO))
    text(target / 'PYTHON_DEPENDENCIES.txt', '\n'.join(name + '==' + version for name, version in versions.items()) + '\n')
    text(target / 'THIRD_PARTY_NOTICES.md', '''# Trial package components

RenderTest is a modified RenderDoc build; see LICENSE.md and docs/credits_acknowledgements.rst.
Native component license texts are under licenses/native. Qt libraries are dynamically linked,
and may be replaced with compatible builds. Python 3.6 is the GUI's embedded runtime; its license
is included under licenses/native. The separate MCP runtime is the official CPython 3.14.7 x64
embeddable release, with its license alongside runtime/python/python.exe.

Python dependencies and versions are listed in PYTHON_DEPENDENCIES.txt; their distribution
metadata and license files remain in runtime/python/Lib/site-packages/*.dist-info.
The Claude Agent SDK Python library is included; its bundled Claude executable is omitted.
Install and authenticate Codex or Claude Code separately under their own terms.
No accounts, API keys, application settings, histories or captures are distributed.

Shader decompiler component sources/releases and hashes are recorded in
integrations/rendertest_agent/tools/shader_decompiler/manifest.json, with licenses beside it.
Microsoft Visual C++ app-local runtime DLLs come from the installed Visual Studio x64 CRT
redistributable directory. Windows system libraries and graphics drivers are not bundled.

Qt version: 5.9.4. Corresponding upstream source packages:
https://download.qt.io/archive/qt/5.9/5.9.4/single/

Python release: https://www.python.org/downloads/release/python-3147/
RenderDoc upstream: https://github.com/baldurk/renderdoc
''')
    revision = subprocess.check_output(['git', '-c', 'safe.directory=' + REPO.as_posix(), 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    files = []
    for path in sorted(target.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(target).as_posix()
        if path.suffix.lower() in ('.pdb', '.lib', '.exp', '.rdc', '.pyc') or '__pycache__' in path.parts:
            raise RuntimeError('Unexpected development/private artifact: ' + relative)
        files.append({'path': relative, 'size': path.stat().st_size, 'sha256': sha(path)})
    text(target / 'PACKAGE_MANIFEST.json', json.dumps({'name': args.name, 'sourceCommit': revision,
         'platform': 'windows-x86_64', 'archiveRoot': 'RenderTest', 'pythonSource': PYTHON_URL, 'pythonArchiveSha256': PYTHON_SHA256,
         'toolCount': 35, 'skillCount': 4, 'dependencies': versions, 'files': files}, ensure_ascii=False, indent=2) + '\n')
    print('Staged:', target, flush=True)
    if not args.no_zip:
        archive = target.parent / (target.name + '.zip')
        if archive.exists():
            raise RuntimeError('Archive already exists: ' + str(archive))
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as stream:
            for path in sorted(target.rglob('*')):
                if path.is_file():
                    stream.write(path, (Path('RenderTest') / path.relative_to(target)).as_posix())
        text(archive.with_suffix('.zip.sha256'), sha(archive) + '  ' + archive.name + '\n')
        print('Archive:', archive, 'bytes=', archive.stat().st_size, flush=True)


if __name__ == '__main__':
    main()
