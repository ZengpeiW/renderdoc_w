import json
import zipfile

import pytest

from package_trial import ANDROID_ABIS, sha, validate_android_apk


def make_apk(tmp_path, abi, *, machine_override=None, omit=None):
    native_abi, elf_class, machine = ANDROID_ABIS[abi]
    path = tmp_path / f'org.renderdoc.renderdoccmd.{abi}.apk'
    header = bytearray(20)
    header[:6] = b'\x7fELF' + bytes((elf_class, 1))
    header[18:20] = (machine if machine_override is None else machine_override).to_bytes(2, 'little')
    entries = {'AndroidManifest.xml': b'manifest', 'classes.dex': b'dex',
               f'lib/{native_abi}/wrap.sh': b'#!/system/bin/sh\n',
               f'lib/{native_abi}/librenderdoccmd.so': header,
               f'lib/{native_abi}/libVkLayer_GLES_RenderDoc.so': header}
    with zipfile.ZipFile(path, 'w') as archive:
        for name, value in entries.items():
            if name != omit:
                archive.writestr(name, value)
    path.with_suffix('.apk.json').write_text(json.dumps({
        'packageName': f'org.renderdoc.renderdoccmd.{abi}', 'abi': native_abi,
        'versionName': 'a' * 40, 'versionCode': '147', 'sha256': sha(path),
    }), encoding='utf-8')
    return path


@pytest.mark.parametrize('abi', ANDROID_ABIS)
def test_matching_apk(tmp_path, abi):
    info = validate_android_apk(make_apk(tmp_path, abi), abi, 'a' * 40, '147')
    assert info['abi'] == ANDROID_ABIS[abi][0]


@pytest.mark.parametrize(('commit', 'version'), [('b' * 40, '147'), ('a' * 40, '146')])
def test_reject_incompatible_pc(tmp_path, commit, version):
    with pytest.raises(RuntimeError, match='does not match the PC library'):
        validate_android_apk(make_apk(tmp_path, 'arm64'), 'arm64', commit, version)


def test_reject_modified_apk(tmp_path):
    path = make_apk(tmp_path, 'arm64')
    with zipfile.ZipFile(path, 'a') as archive:
        archive.writestr('unexpected', b'changed after validation')
    with pytest.raises(RuntimeError, match='does not match the PC library'):
        validate_android_apk(path, 'arm64', 'a' * 40, '147')


def test_reject_wrong_native_architecture(tmp_path):
    with pytest.raises(RuntimeError, match='Wrong ELF architecture'):
        validate_android_apk(make_apk(tmp_path, 'arm64', machine_override=62), 'arm64', 'a' * 40, '147')


def test_reject_missing_dex(tmp_path):
    with pytest.raises(KeyError, match='classes.dex'):
        validate_android_apk(make_apk(tmp_path, 'arm64', omit='classes.dex'), 'arm64', 'a' * 40, '147')
