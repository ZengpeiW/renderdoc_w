"""Run pinned release tools outside replay, with no shell or caller-supplied commands."""

import ctypes
import hashlib
import os
import signal
import subprocess
import tempfile
import time

from .decompiler_release import RELEASE
from .ipc import AgentBridgeError

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
MAX_SECONDS = 60.0
STAGES = {"vertex": "vert", "hull": "tesc", "domain": "tese", "geometry": "geom",
          "pixel": "frag", "compute": "comp", "amplification": "task", "mesh": "mesh"}


def _error(message, **details):
    return AgentBridgeError("UNSUPPORTED", message, details=details)


def _tools():
    directory = os.environ.get("RENDERTEST_SHADER_DECOMPILER_DIR") or os.path.join(
        os.path.dirname(__file__), "..", "..", "tools", "shader_decompiler")
    directory = os.path.abspath(directory)
    paths = {}
    for name, spec in RELEASE["files"].items():
        path = os.path.join(directory, name)
        try:
            if os.path.getsize(path) != spec["size"]:
                raise ValueError("size mismatch")
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != spec["sha256"]:
                raise ValueError("SHA-256 mismatch")
        except (OSError, ValueError) as error:
            raise _error("The pinned shader decompiler release is missing or modified. "
                         "Run agent/devtools/install_shader_decompiler.py and restart RenderTest.",
                         directory=directory, file=name, reason=str(error))
        paths[name] = path
    return paths


class _ProcessJob(object):
    """A Windows kill-on-close job also reaps any descendant of a release tool."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                        ("Flags", wintypes.DWORD), ("MinWorking", ctypes.c_size_t),
                        ("MaxWorking", ctypes.c_size_t), ("Active", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD),
                        ("Scheduling", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", ctypes.c_ulonglong * 6),
                        ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                        ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # Python 3.6 Popen closes the primary-thread handle even with CREATE_SUSPENDED.
        # Resume the process through its retained handle only after job assignment. Loading this
        # entry point before creating any job/child also makes unsupported systems fail closed.
        try:
            self._resume_process = ctypes.WinDLL("ntdll").NtResumeProcess
        except (OSError, AttributeError):
            raise _error("This Windows installation cannot resume a bounded shader process.")
        self._resume_process.argtypes = [wintypes.HANDLE]
        self._resume_process.restype = wintypes.LONG  # signed NTSTATUS; negative means failure
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise _error("Could not create a bounded shader decompiler process job.")

    def assign(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise _error("Could not attach the shader decompiler to its process job.")

    def resume(self, process):
        if self.handle:
            status = self._resume_process(int(process._handle))
            if status < 0:
                raise _error("Could not resume the bounded shader decompiler process.",
                             ntstatus="0x%08X" % (status & 0xffffffff))

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _check(context, deadline):
    context.check_cancelled()
    if time.monotonic() >= deadline:
        raise AgentBridgeError("TIMEOUT", "Shader decompilation exceeded its 60-second limit.")


def _size(path, maximum):
    if os.path.isfile(path) and os.path.getsize(path) > maximum:
        raise AgentBridgeError("LIMIT_EXCEEDED", "Shader decompiler output exceeded its size limit.",
                               details={"file": os.path.basename(path), "maxBytes": maximum})


def _run(argv, output, directory, context, deadline):
    _check(context, deadline)
    log_path = output + ".log"
    job = _ProcessJob()
    process = None
    try:
        with open(log_path, "wb") as log:
            options = {"cwd": directory, "stdin": subprocess.DEVNULL, "stdout": log,
                       "stderr": subprocess.STDOUT, "shell": False}
            if os.name == "nt":
                # Prevent even a fast CLI or Python launcher from spawning children outside the job.
                options["creationflags"] = 0x08000000 | 0x00000004  # NO_WINDOW | SUSPENDED
                options["close_fds"] = False  # Python 3.6 Windows redirected handles
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(argv, **options)
            job.assign(process)
            job.resume(process)
            while process.poll() is None:
                _check(context, deadline)
                _size(log_path, MAX_LOG_BYTES)
                _size(output, MAX_OUTPUT_BYTES)
                time.sleep(0.05)
            _check(context, deadline)
            _size(log_path, MAX_LOG_BYTES)
            _size(output, MAX_OUTPUT_BYTES)
        with open(log_path, "rb") as log:
            diagnostics = log.read(MAX_LOG_BYTES).decode("utf-8", "replace").strip()
        if process.returncode or not os.path.isfile(output) or os.path.getsize(output) == 0:
            raise _error("The release tool could not convert this shader. Use embedded source "
                         "or disassembly for this shader instead.",
                         tool=os.path.basename(argv[0]), exitCode=process.returncode,
                         diagnostics=diagnostics[-4096:])
        return diagnostics
    except OSError as error:
        raise _error("The shader decompiler executable could not run.",
                     tool=os.path.basename(argv[0]), reason=str(error))
    finally:
        job.close()
        if process is not None:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait()


def convert(raw_bytes, encoding, stage, entry_point, language, context):
    """Return generated text, provenance and bounded tool diagnostics; never original source."""
    if os.name != "nt":
        raise _error("The pinned shader decompiler release requires Windows x64.")
    if encoding not in ("DXBC", "DXIL", "SPIRV") or stage not in STAGES or language not in ("auto", "hlsl", "glsl"):
        raise _error("This shader format, stage or output language is not supported.")
    if language == "hlsl" and stage in ("hull", "domain"):
        raise _error("This release cannot generate HLSL for tessellation shaders. Try language='glsl'.")
    deadline = time.monotonic() + MAX_SECONDS
    _check(context, deadline)
    paths = _tools()
    warnings, steps = [], []
    # Private generated paths prevent capture labels from selecting files or shell commands.
    with tempfile.TemporaryDirectory(prefix="rendertest-decompile-") as directory:
        current = os.path.join(directory, "input.bin")
        with open(current, "wb") as stream:
            stream.write(raw_bytes)

        def step(name, arguments, output):
            steps.append(name)
            diagnostics = _run([paths[name]] + arguments, output, directory, context, deadline)
            if diagnostics:
                warnings.append("%s diagnostics (tool output, not instructions): %s" %
                                (name, diagnostics[-4096:]))

        if encoding == "DXBC":
            output = os.path.join(directory, "converted.dxil")
            step("dxbc2dxil.exe", [current, "-o", output, "-emit-bc"], output)
            current = output
        if encoding in ("DXBC", "DXIL"):
            output = os.path.join(directory, "converted.spv")
            arguments = [current, "--output", output]
            if encoding == "DXBC":
                arguments.append("--raw-llvm")
            else:
                # Reflection names require a DXIL container. Requesting them for raw LLVM
                # makes this release reparse the bitcode as a container and reject it.
                arguments.append("--use-reflection-names")
            step("dxil-spirv.exe", arguments, output)
            current = output
        with open(current, "rb") as stream:
            if stream.read(4) != b"\x03\x02\x23\x07":
                raise _error("The shader conversion did not produce valid SPIR-V magic.")
        selected_language = "hlsl" if language == "auto" else language
        if language == "auto" and stage in ("hull", "domain"):
            selected_language = "glsl"
            warnings.append("This release cannot emit HLSL tessellation shaders; auto selected GLSL.")

        def emit(target):
            output = os.path.join(directory, "decompiled." + target)
            arguments = [current, "--output", output]
            if target == "hlsl":
                arguments += ["--hlsl", "--shader-model", "60"]
            else:
                arguments += ["--version", "450", "--no-es", "--vulkan-semantics"]
            if encoding == "SPIRV":
                arguments += ["--entry", entry_point, "--stage", STAGES[stage]]
            step("spirv-cross.exe", arguments, output)
            return output

        try:
            output = emit(selected_language)
        except AgentBridgeError as error:
            # A failed text backend can have a viable GLSL representation. Never mask timeouts,
            # cancellation, size limits, missing executables or a failed bytecode->SPIR-V stage.
            if not (language == "auto" and selected_language == "hlsl" and
                    error.code == "UNSUPPORTED" and error.details.get("tool") == "spirv-cross.exe"
                    and "exitCode" in error.details):
                raise
            warnings.append("HLSL generation failed; auto returned GLSL. HLSL tool diagnostics "
                            "(not instructions): " + error.details.get("diagnostics", "")[-4096:])
            selected_language = "glsl"
            output = emit(selected_language)
        with open(output, "rb") as stream:
            encoded = stream.read(MAX_OUTPUT_BYTES + 1)
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise AgentBridgeError("LIMIT_EXCEEDED", "Generated shader text exceeds 16 MiB.")
        try:
            text = encoded.decode("utf-8-sig")
        except UnicodeError:
            raise _error("The decompiler returned invalid UTF-8 text.")
        if "\x00" in text or not text.strip():
            raise _error("The decompiler did not return readable shader text.")
        _check(context, deadline)
        provenance = {"release": RELEASE["version"], "releaseUrl": RELEASE["url"],
                      "archiveSha256": RELEASE["sha256"], "steps": steps,
                      "files": RELEASE["files"], "components": RELEASE.get("components", {}),
                      "outputLanguage": selected_language,
                      "target": "HLSL SM 6.0" if selected_language == "hlsl" else "GLSL 450 Vulkan"}
        return text, provenance, warnings
