from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import sys
import time

import pytest

from rendertest_agent_bridge import shader_decompiler as sd
from rendertest_agent_bridge.ipc import AgentBridgeError


class Context:
    def check_cancelled(self):
        pass


def test_installed_tools_require_each_pinned_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDERTEST_SHADER_DECOMPILER_DIR", str(tmp_path))
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"release")
    monkeypatch.setattr(sd, "RELEASE", {"files": {"tool.exe": {
        "size": 7, "sha256": hashlib.sha256(b"release").hexdigest()}}})
    assert sd._tools() == {"tool.exe": str(executable)}
    executable.write_bytes(b"changed")
    with pytest.raises(AgentBridgeError) as error:
        sd._tools()
    assert error.value.code == "UNSUPPORTED" and "modified" in str(error.value)
    executable.unlink()
    with pytest.raises(AgentBridgeError):
        sd._tools()


@pytest.mark.parametrize("encoding,expected", [
    ("DXBC", ["dxbc2dxil.exe", "dxil-spirv.exe", "spirv-cross.exe"]),
    ("DXIL", ["dxil-spirv.exe", "spirv-cross.exe"]),
    ("SPIRV", ["spirv-cross.exe"]),
])
@pytest.mark.parametrize("language", ["hlsl", "glsl"])
def test_conversion_chain_uses_private_paths_and_exact_format_flags(monkeypatch, encoding, expected, language):
    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "_tools", lambda: {name: name for name in expected})
    calls, directories = [], []

    def run(argv, output, directory, context, deadline):
        calls.append(argv)
        directories.append(directory)
        Path(output).write_bytes(b"// generated\nvoid main() {}" if argv[0] == "spirv-cross.exe"
                                 else b"\x03\x02\x23\x07" + b"\0" * 16)
        return ""

    monkeypatch.setattr(sd, "_run", run)
    # Entry names are argv values, even when they resemble shell syntax or CLI options.
    entry = 'main&echo "unexpected"'
    text, provenance, warnings = sd.convert(b"\x03\x02\x23\x07" + b"\0" * 16,
                                           encoding, "pixel", entry, language, Context())
    assert [args[0] for args in calls] == expected
    assert provenance["steps"] == expected and text.startswith("// generated") and not warnings
    if encoding == "DXBC":
        assert "-emit-bc" in calls[0] and "--raw-llvm" in calls[1]
        assert "--use-reflection-names" not in calls[1]
    elif encoding == "DXIL":
        assert "--raw-llvm" not in calls[0]
        assert "--use-reflection-names" in calls[0]
    if encoding == "SPIRV":
        assert calls[-1][-4:] == ["--entry", entry, "--stage", "frag"]
    assert ("--hlsl" in calls[-1]) == (language == "hlsl")
    assert all(not Path(directory).exists() for directory in directories)


def test_conversion_error_removes_intermediates(monkeypatch):
    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "_tools", lambda: {"dxil-spirv.exe": "tool"})
    directories = []

    def fail(argv, output, directory, *rest):
        directories.append(directory)
        Path(output).write_bytes(b"partial")
        raise AgentBridgeError("UNSUPPORTED", "unsupported opcode")

    monkeypatch.setattr(sd, "_run", fail)
    with pytest.raises(AgentBridgeError):
        sd.convert(b"input", "DXIL", "pixel", "main", "hlsl", Context())
    assert all(not Path(directory).exists() for directory in directories)


def test_hlsl_tessellation_fails_before_launch(monkeypatch):
    monkeypatch.setattr(sd.os, "name", "nt")
    with pytest.raises(AgentBridgeError, match="language='glsl'"):
        sd.convert(b"input", "DXIL", "hull", "main", "hlsl", Context())


@pytest.mark.parametrize("failure_code", ["UNSUPPORTED", "TIMEOUT", "CANCELLED", "LIMIT_EXCEEDED"])
def test_auto_falls_back_only_for_text_backend_rejection(monkeypatch, failure_code):
    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "_tools", lambda: {"spirv-cross.exe": "spirv-cross.exe"})
    calls = []

    def run(argv, output, directory, context, deadline):
        calls.append(argv)
        if "--hlsl" in argv:
            raise AgentBridgeError(failure_code, "not expressible", details={
                "tool": "spirv-cross.exe", "exitCode": 1, "diagnostics": "BallotFindLSB"})
        Path(output).write_text("#version 450\nvoid main() {}", encoding="utf-8")
        return ""

    monkeypatch.setattr(sd, "_run", run)
    if failure_code == "UNSUPPORTED":
        text, provenance, warnings = sd.convert(b"\x03\x02\x23\x07" + b"\0" * 16,
                                               "SPIRV", "pixel", "main", "auto", Context())
        assert len(calls) == 2 and provenance["outputLanguage"] == "glsl"
        assert text.startswith("#version") and "BallotFindLSB" in warnings[0]
    else:
        with pytest.raises(AgentBridgeError) as error:
            sd.convert(b"\x03\x02\x23\x07" + b"\0" * 16, "SPIRV", "pixel", "main", "auto", Context())
        assert error.value.code == failure_code and len(calls) == 1


def test_auto_does_not_retry_failed_bytecode_translation(monkeypatch):
    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "_tools", lambda: {"dxil-spirv.exe": "dxil-spirv.exe"})
    calls = []

    def run(argv, *rest):
        calls.append(argv)
        raise AgentBridgeError("UNSUPPORTED", "bad bytecode", details={"tool": "dxil-spirv.exe", "exitCode": 1})

    monkeypatch.setattr(sd, "_run", run)
    with pytest.raises(AgentBridgeError):
        sd.convert(b"DXIL", "DXIL", "pixel", "main", "auto", Context())
    assert len(calls) == 1


def test_auto_tessellation_selects_glsl_directly(monkeypatch):
    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "_tools", lambda: {"spirv-cross.exe": "spirv-cross.exe"})
    calls = []

    def run(argv, output, *rest):
        calls.append(argv)
        Path(output).write_text("#version 450\nvoid main() {}", encoding="utf-8")
        return ""

    monkeypatch.setattr(sd, "_run", run)
    _, provenance, warnings = sd.convert(b"\x03\x02\x23\x07" + b"\0" * 16,
                                         "SPIRV", "domain", "main", "auto", Context())
    assert len(calls) == 1 and "--hlsl" not in calls[0]
    assert provenance["outputLanguage"] == "glsl" and warnings


def run_python(tmp_path, code, context=None, seconds=5):
    output = tmp_path / "output.txt"
    return sd._run([sys.executable, "-c", code, str(output)], str(output), str(tmp_path),
                   context or Context(), time.monotonic() + seconds)


def test_real_process_success_and_nonzero_diagnostics(tmp_path):
    assert run_python(tmp_path, "import sys; open(sys.argv[1], 'w').write('result')") == ""
    with pytest.raises(AgentBridgeError) as error:
        run_python(tmp_path, "import sys; print('unsupported test opcode'); sys.exit(3)")
    assert error.value.code == "UNSUPPORTED"
    assert error.value.details["exitCode"] == 3
    assert "unsupported test opcode" in error.value.details["diagnostics"]


def test_process_deadline_and_output_limit(tmp_path, monkeypatch):
    start = time.monotonic()
    with pytest.raises(AgentBridgeError) as error:
        run_python(tmp_path, "import time; time.sleep(30)", seconds=.25)
    assert error.value.code == "TIMEOUT" and time.monotonic() - start < 5
    monkeypatch.setattr(sd, "MAX_OUTPUT_BYTES", 1024)
    with pytest.raises(AgentBridgeError) as error:
        run_python(tmp_path, "import sys,time; open(sys.argv[1],'wb').write(b'x'*8192); time.sleep(30)")
    assert error.value.code == "LIMIT_EXCEEDED"


def test_process_cancel_and_log_limit(tmp_path, monkeypatch):
    class Cancel:
        calls = 0

        def check_cancelled(self):
            self.calls += 1
            if self.calls > 3:
                raise AgentBridgeError("CANCELLED", "test stop")

    with pytest.raises(AgentBridgeError) as error:
        run_python(tmp_path, "import time; time.sleep(30)", Cancel())
    assert error.value.code == "CANCELLED"
    monkeypatch.setattr(sd, "MAX_LOG_BYTES", 1024)
    with pytest.raises(AgentBridgeError) as error:
        run_python(tmp_path, "import sys,time; print('x'*8192,flush=True); time.sleep(30)")
    assert error.value.code == "LIMIT_EXCEEDED"


@pytest.mark.skipif(os.name != "nt", reason="Windows release process job")
def test_timeout_terminates_descendant_process(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = ("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "open(%r,'w').write(str(p.pid)); time.sleep(30)" % str(pid_file))
    with pytest.raises(AgentBridgeError) as error:
        run_python(tmp_path, code, seconds=.6)
    assert error.value.code == "TIMEOUT" and pid_file.exists()
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 2000) == 0
        finally:
            kernel.CloseHandle(handle)


def assert_windows_child_stopped(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100001, False, pid)  # synchronize + terminate (failure cleanup)
    if not handle:
        assert ctypes.get_last_error() == 87  # no such process
        return
    try:
        ended = kernel.WaitForSingleObject(handle, 2000) == 0
        if not ended:
            # Keep a failed regression from leaving our own test child alive.
            kernel.TerminateProcess(handle, 1)
            kernel.WaitForSingleObject(handle, 2000)
        assert ended, "decompiler descendant survived process-job cleanup"
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process/job ordering")
def test_fast_parent_cannot_spawn_before_job_assignment_and_descendant_is_reaped(tmp_path, monkeypatch):
    pid_file = tmp_path / "fast-child.pid"
    original_assign = sd._ProcessJob.assign

    def delayed_assign(job, process):
        # Make the old launch-then-assign race deterministic: an unsuspended parent would already
        # have spawned its child and exited by this point. A suspended child cannot run any code.
        time.sleep(.25)
        assert process.poll() is None and not pid_file.exists()
        original_assign(job, process)

    monkeypatch.setattr(sd._ProcessJob, "assign", delayed_assign)
    code = ("import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "open(%r,'w').write(str(p.pid)); open(sys.argv[1],'w').write('result')" % str(pid_file))
    try:
        assert run_python(tmp_path, code) == ""
        assert pid_file.exists()
    finally:
        if pid_file.exists():
            assert_windows_child_stopped(int(pid_file.read_text()))


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process cleanup")
@pytest.mark.parametrize("failed_phase", ["assign", "resume"])
def test_failed_job_assignment_or_resume_kills_suspended_child(tmp_path, monkeypatch, failed_phase):
    started = tmp_path / "must-not-start.txt"
    processes = []
    original_popen = sd.subprocess.Popen

    def remember(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail(job, process):
        raise AgentBridgeError("UNSUPPORTED", "simulated " + failed_phase + " failure")

    monkeypatch.setattr(sd.subprocess, "Popen", remember)
    monkeypatch.setattr(sd._ProcessJob, failed_phase, fail)
    with pytest.raises(AgentBridgeError, match="simulated"):
        run_python(tmp_path, "import time; open(%r,'w').write('started'); time.sleep(30)" % str(started))
    assert processes and all(process.poll() is not None for process in processes)
    assert not started.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows NTSTATUS validation")
def test_failed_ntstatus_is_checked_and_job_closed():
    job = sd._ProcessJob()
    try:
        job._resume_process = lambda handle: -1073741816  # STATUS_INVALID_HANDLE
        with pytest.raises(AgentBridgeError) as raised:
            job.resume(type("Process", (), {"_handle": 123})())
        assert raised.value.code == "UNSUPPORTED"
        assert raised.value.details["ntstatus"].startswith("0xC")
    finally:
        job.close()
    assert job.handle is None


def test_runner_keeps_embedded_python36_syntax():
    ast.parse(Path(sd.__file__).read_text(), feature_version=(3, 6))
