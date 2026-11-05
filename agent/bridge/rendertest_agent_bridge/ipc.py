import json
import os
import queue
import re
import threading
import time
import traceback

from .constants import (
    BRIDGE_VERSION,
    DEFAULT_QUEUE_LIMIT,
    ERROR_RETRYABLE,
    HEARTBEAT_INTERVAL_SECONDS,
    INSTANCE_SCHEMA,
    MAX_DEADLINE_MS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    METHOD_SPECS,
    PROTOCOL,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    STALE_RESPONSE_SECONDS,
)
from .paths import instances_root

REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
REQUEST_FILE_PATTERN = re.compile(r"^([0-9a-f]{32})\.json$")
SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AgentBridgeError(Exception):
    def __init__(self, code, message, retryable=None, details=None):
        if code not in ERROR_RETRYABLE:
            code = "INTERNAL_ERROR"
        Exception.__init__(self, message)
        self.code = code
        self.message = str(message)
        self.retryable = ERROR_RETRYABLE[code] if retryable is None else bool(retryable)
        self.details = details or {}

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class BridgeResult(object):
    def __init__(self, result, meta=None):
        self.result = result
        self.meta = meta or {}


class RequestContext(object):
    def __init__(self, server, request_id, method, params, effect, deadline_at, state):
        self.server = server
        self.request_id = request_id
        self.method = method
        self.params = params
        self.effect = effect
        self.deadline_at = deadline_at
        self.state = state
        self.cancel_event = server._stop_event

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise AgentBridgeError("CANCELLED", "The agent bridge is stopping.")
        if time.time() > self.deadline_at:
            raise AgentBridgeError("TIMEOUT", "The request exceeded its deadline.")


def utc_iso(timestamp):
    whole = int(timestamp)
    millis = int((timestamp - whole) * 1000)
    return "%s.%03dZ" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)), millis)


def unix_ms(timestamp):
    return int(timestamp * 1000)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8")


def atomic_write_bytes(path, data):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(temporary, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())

    last_error = None
    for _ in range(50):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(0.01)
    try:
        os.remove(temporary)
    except OSError:
        pass
    raise last_error


def atomic_write_json(path, value):
    atomic_write_bytes(path, json_bytes(value))


def read_json_file(path, max_bytes):
    last_error = None
    for _ in range(20):
        try:
            if os.path.getsize(path) > max_bytes:
                raise AgentBridgeError("LIMIT_EXCEEDED", "The JSON file is too large.",
                                       details={"maxBytes": max_bytes})
            with open(path, "rb") as stream:
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise AgentBridgeError("LIMIT_EXCEEDED", "The JSON file is too large.",
                                       details={"maxBytes": max_bytes})
            return json.loads(data.decode("utf-8"))
        except PermissionError as error:
            last_error = error
            time.sleep(0.01)
    raise last_error


def _require_int(value, label, minimum=None, maximum=None):
    if type(value) is not int:
        raise AgentBridgeError("INVALID_REQUEST", "%s must be an integer." % label)
    if minimum is not None and value < minimum:
        raise AgentBridgeError("INVALID_REQUEST", "%s is below its minimum." % label)
    if maximum is not None and value > maximum:
        raise AgentBridgeError("INVALID_REQUEST", "%s exceeds its maximum." % label)
    return value


def validate_request(request, request_id, supported_methods):
    if not isinstance(request, dict):
        raise AgentBridgeError("INVALID_REQUEST", "The request root must be an object.")
    if request.get("schema") != REQUEST_SCHEMA:
        raise AgentBridgeError("INVALID_REQUEST", "The request schema is not supported.")
    if request.get("protocol") != PROTOCOL:
        raise AgentBridgeError("INVALID_REQUEST", "The request protocol is not supported.")
    if request.get("id") != request_id or not REQUEST_ID_PATTERN.match(str(request_id)):
        raise AgentBridgeError("INVALID_REQUEST", "The request id does not match its file name.")

    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise AgentBridgeError("INVALID_REQUEST", "The request method must be a string.")
    if method not in supported_methods:
        raise AgentBridgeError("UNKNOWN_METHOD", "The bridge does not support this method.",
                               details={"method": method})

    params = request.get("params")
    if not isinstance(params, dict):
        raise AgentBridgeError("INVALID_REQUEST", "Request params must be an object.")

    created_ms = _require_int(request.get("createdAtUnixMs"), "createdAtUnixMs", 1)
    deadline_ms = _require_int(request.get("deadlineMs"), "deadlineMs", 1, MAX_DEADLINE_MS)

    expected = request.get("expected", {})
    if expected is None:
        expected = {}
    if not isinstance(expected, dict):
        raise AgentBridgeError("INVALID_REQUEST", "Request expected bindings must be an object.")
    session = expected.get("captureSessionId")
    if session is not None and (not isinstance(session, str) or
                                not SESSION_ID_PATTERN.match(session)):
        raise AgentBridgeError("INVALID_REQUEST", "The expected capture session is invalid.")
    instance = expected.get("instanceId")
    if instance is not None and not isinstance(instance, str):
        raise AgentBridgeError("INVALID_REQUEST", "The expected instance id is invalid.")

    return method, params, expected, (created_ms + deadline_ms) / 1000.0


class MailboxServer(object):
    def __init__(self, root, instance_id, handler, state_provider, methods=None,
                 bridge_version=BRIDGE_VERSION, renderdoc_version="", pid=None,
                 queue_limit=DEFAULT_QUEUE_LIMIT, stale_response_seconds=STALE_RESPONSE_SECONDS,
                 log=None):
        if not INSTANCE_ID_PATTERN.match(str(instance_id or "")):
            raise ValueError("Invalid agent bridge instance id: %r" % instance_id)
        self.root = os.path.abspath(root)
        self.instance_id = instance_id
        self.instance_dir = os.path.join(instances_root(self.root), instance_id)
        self.request_dir = os.path.join(self.instance_dir, "requests")
        self.processing_dir = os.path.join(self.instance_dir, "processing")
        self.response_dir = os.path.join(self.instance_dir, "responses")
        self.manifest_path = os.path.join(self.instance_dir, "instance.json")
        self.handler = handler
        self.state_provider = state_provider
        self.methods = tuple(sorted(methods or METHOD_SPECS.keys()))
        unknown = [method for method in self.methods if method not in METHOD_SPECS]
        if unknown:
            raise ValueError("Unknown agent bridge methods: %s" % ", ".join(unknown))
        self.bridge_version = bridge_version
        self.renderdoc_version = renderdoc_version
        self.pid = os.getpid() if pid is None else pid
        self.stale_response_seconds = stale_response_seconds
        self._log = log or (lambda message: None)
        self._queue = queue.Queue(queue_limit)
        self._stop_event = threading.Event()
        self._manifest_lock = threading.Lock()
        self._worker = None
        self._accepting = False
        self._started_at = time.time()
        self._last_activity_at = self._started_at
        self._last_manifest_at = 0.0

    def start(self, start_worker=True):
        for directory in (self.instance_dir, self.request_dir, self.processing_dir,
                          self.response_dir):
            if not os.path.isdir(directory):
                os.makedirs(directory)
        self._stop_event.clear()
        self._accepting = True
        self._started_at = time.time()
        self._last_activity_at = self._started_at
        self.recover_processing()
        self.cleanup_stale_responses()
        self.publish_manifest(activity=True)
        if start_worker:
            self._worker = threading.Thread(target=self._worker_main,
                                            name="RenderTestAgentBridgeWorker")
            self._worker.daemon = True
            self._worker.start()

    def stop(self, timeout=5.0):
        self._accepting = False
        self._stop_event.set()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self._write_error(item["requestId"], item["processingPath"],
                                  AgentBridgeError("CANCELLED", "The agent bridge is stopping."),
                                  "", time.time())
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        stopped = True
        if self._worker is not None:
            self._worker.join(timeout)
            if self._worker.is_alive():
                stopped = False
            else:
                self._worker = None
        self.publish_manifest(stopped=True)
        return stopped

    def notify_activity(self):
        self.publish_manifest(activity=True)

    def publish_manifest(self, activity=False, stopped=False):
        now = time.time()
        if activity:
            self._last_activity_at = now
        state = self._state()
        manifest = {
            "schema": INSTANCE_SCHEMA,
            "protocol": PROTOCOL,
            "instanceId": self.instance_id,
            "pid": self.pid,
            "bridgeVersion": self.bridge_version,
            "renderdocVersion": self.renderdoc_version,
            "startedAt": utc_iso(self._started_at),
            "startedAtUnixMs": unix_ms(self._started_at),
            "heartbeatAt": utc_iso(now),
            "heartbeatUnixMs": unix_ms(now),
            "lastActivityAt": utc_iso(self._last_activity_at),
            "lastActivityUnixMs": unix_ms(self._last_activity_at),
            "captureLoaded": bool(state.get("captureLoaded")),
            "capturePath": state.get("capturePath") or "",
            "captureSessionId": state.get("captureSessionId") or "",
            "api": state.get("api") or "",
            "selectedEventId": state.get("selectedEventId"),
            "currentEventId": state.get("currentEventId"),
            "methods": list(self.methods),
            "stopped": bool(stopped),
        }
        if stopped:
            manifest["captureLoaded"] = False
            manifest["stoppedAt"] = utc_iso(now)
            manifest["stoppedAtUnixMs"] = unix_ms(now)
        with self._manifest_lock:
            atomic_write_json(self.manifest_path, manifest)
            self._last_manifest_at = now

    def poll_once(self):
        if not self._accepting:
            return 0
        now = time.time()
        if now - self._last_manifest_at >= HEARTBEAT_INTERVAL_SECONDS:
            self.publish_manifest()

        claimed = 0
        try:
            names = sorted(os.listdir(self.request_dir))
        except OSError:
            return 0
        for name in names:
            match = REQUEST_FILE_PATTERN.match(name)
            if not match:
                continue
            request_id = match.group(1)
            source = os.path.join(self.request_dir, name)
            target = os.path.join(self.processing_dir, name)
            try:
                os.replace(source, target)
            except (FileNotFoundError, PermissionError):
                continue
            item = {"requestId": request_id, "processingPath": target,
                    "claimedAt": time.time()}
            try:
                self._queue.put_nowait(item)
                claimed += 1
            except queue.Full:
                self._write_error(request_id, target,
                                  AgentBridgeError("BRIDGE_BUSY", "The agent bridge queue is full."),
                                  "", item["claimedAt"])
        return claimed

    def process_next(self, timeout=0.0):
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        if item is None:
            return False
        self._process_item(item)
        return True

    def recover_processing(self):
        for name in os.listdir(self.processing_dir):
            match = REQUEST_FILE_PATTERN.match(name)
            if not match:
                continue
            self._write_error(match.group(1), os.path.join(self.processing_dir, name),
                              AgentBridgeError("BRIDGE_RESTARTED",
                                               "The agent bridge restarted during this request."),
                              "", time.time())

    def cleanup_stale_responses(self):
        cutoff = time.time() - self.stale_response_seconds
        for directory in (self.request_dir, self.response_dir):
            for name in os.listdir(directory):
                if not (name.endswith(".json") or name.endswith(".tmp")):
                    continue
                path = os.path.join(directory, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass

    def _worker_main(self):
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            self._process_item(item)

    def _state(self):
        try:
            state = self.state_provider() or {}
        except Exception as error:
            self._log("Agent bridge state provider failed: %s" % error)
            state = {}
        if not isinstance(state, dict):
            state = {}
        return state

    def _process_item(self, item):
        started = time.time()
        request_id = item["requestId"]
        processing_path = item["processingPath"]
        method = ""
        try:
            request = read_json_file(processing_path, MAX_REQUEST_BYTES)
            method, params, expected, deadline_at = validate_request(
                request, request_id, self.methods)
            spec = METHOD_SPECS[method]
            if time.time() > deadline_at:
                raise AgentBridgeError("TIMEOUT", "The request expired before execution.")
            if self._stop_event.is_set():
                raise AgentBridgeError("CANCELLED", "The agent bridge is stopping.")

            expected_instance = expected.get("instanceId")
            if expected_instance is not None and expected_instance != self.instance_id:
                raise AgentBridgeError("INVALID_REQUEST",
                                       "The request was addressed to another instance.")

            before = self._state()
            before_session = before.get("captureSessionId") or ""
            if spec["requiresCapture"] and not before.get("captureLoaded"):
                raise AgentBridgeError("NO_CAPTURE", "No capture is currently loaded.")
            expected_session = expected.get("captureSessionId")
            if expected_session is not None and expected_session != before_session:
                raise AgentBridgeError("CAPTURE_MISMATCH",
                                       "The loaded capture differs from the requested session.",
                                       details={"expectedCaptureSessionId": expected_session,
                                                "actualCaptureSessionId": before_session})

            context = RequestContext(self, request_id, method, params, spec["effect"],
                                     deadline_at, before)
            value = self.handler(context)
            after = self._state()
            after_session = after.get("captureSessionId") or ""
            if spec["requiresCapture"] and (not after.get("captureLoaded") or
                                            after_session != before_session):
                raise AgentBridgeError("CAPTURE_CHANGED",
                                       "The capture changed while the request was running.")

            result_meta = {}
            if isinstance(value, BridgeResult):
                result_meta = value.meta
                value = value.result
            meta = {
                "instanceId": self.instance_id,
                "captureSessionId": after_session,
                "method": method,
                "effect": spec["effect"],
                "elapsedMs": int((time.time() - started) * 1000),
                "truncated": False,
            }
            meta.update(result_meta)
            response = {
                "schema": RESPONSE_SCHEMA,
                "protocol": PROTOCOL,
                "id": request_id,
                "ok": True,
                "result": value,
                "meta": meta,
            }
            self._write_response(request_id, processing_path, response, method, started)
        except AgentBridgeError as error:
            self._write_error(request_id, processing_path, error, method, started)
        except Exception as error:
            self._log("Agent bridge request failed:\n%s" % traceback.format_exc())
            self._write_error(request_id, processing_path,
                              AgentBridgeError("INTERNAL_ERROR", str(error)[:1024]), method,
                              started)

    def _write_error(self, request_id, processing_path, error, method, started):
        state = self._state()
        response = {
            "schema": RESPONSE_SCHEMA,
            "protocol": PROTOCOL,
            "id": request_id,
            "ok": False,
            "error": error.to_dict(),
            "meta": {
                "instanceId": self.instance_id,
                "captureSessionId": state.get("captureSessionId") or "",
                "method": method,
                "elapsedMs": int((time.time() - started) * 1000),
            },
        }
        self._write_response(request_id, processing_path, response, method, started)

    def _write_response(self, request_id, processing_path, response, method, started):
        response_path = os.path.join(self.response_dir, "%s.json" % request_id)
        data = json_bytes(response)
        if len(data) > MAX_RESPONSE_BYTES:
            error = AgentBridgeError("LIMIT_EXCEEDED", "The response is too large.",
                                     details={"maxBytes": MAX_RESPONSE_BYTES})
            response = {
                "schema": RESPONSE_SCHEMA,
                "protocol": PROTOCOL,
                "id": request_id,
                "ok": False,
                "error": error.to_dict(),
                "meta": {
                    "instanceId": self.instance_id,
                    "method": method,
                    "elapsedMs": int((time.time() - started) * 1000),
                },
            }
            data = json_bytes(response)
        atomic_write_bytes(response_path, data)
        try:
            os.remove(processing_path)
        except OSError:
            pass
