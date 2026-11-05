import io
import os
import shutil
import threading
import time
import uuid

from .constants import BRIDGE_VERSION
from .ipc import AgentBridgeError, MailboxServer, read_json_file
from .lifecycle import CaptureState
from .methods import HANDLERS
from .paths import agent_root, instances_root

POLL_INTERVAL_SECONDS = 0.05
STOP_TIMEOUT_SECONDS = 5.0
PRUNE_STOPPED_AFTER_SECONDS = 60 * 60
PRUNE_SILENT_AFTER_SECONDS = 7 * 24 * 60 * 60
MAX_LOG_BYTES = 4 * 1024 * 1024


def make_instance_id(pid=None):
    return "%d-%s" % (os.getpid() if pid is None else pid, uuid.uuid4().hex[:8])


class BridgeLog(object):
    def __init__(self, path, echo=None):
        self.path = path
        self.echo = echo
        self._lock = threading.Lock()

    def __call__(self, message):
        line = "%s [%d] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), message)
        with self._lock:
            try:
                directory = os.path.dirname(self.path)
                if not os.path.isdir(directory):
                    os.makedirs(directory)
                if os.path.isfile(self.path) and os.path.getsize(self.path) > MAX_LOG_BYTES:
                    os.replace(self.path, self.path + ".1")
                with io.open(self.path, "a", encoding="utf-8") as stream:
                    stream.write(line)
            except Exception:
                pass
        if self.echo is not None:
            try:
                self.echo(message)
            except Exception:
                pass


def prune_instances(root, keep_instance_id, now=None):
    """Remove mailbox directories left by stopped or long-silent instances."""
    now = time.time() if now is None else now
    base = os.path.realpath(instances_root(root))
    if not os.path.isdir(base):
        return []
    removed = []
    for name in os.listdir(base):
        if name == keep_instance_id:
            continue
        directory = os.path.realpath(os.path.join(base, name))
        if os.path.dirname(directory) != base or not os.path.isdir(directory):
            continue
        manifest_path = os.path.join(directory, "instance.json")
        try:
            manifest = read_json_file(manifest_path, 1024 * 1024)
            heartbeat = float(manifest.get("heartbeatUnixMs", 0)) / 1000.0
            stopped = manifest.get("stopped") is True
        except Exception:
            try:
                heartbeat = os.path.getmtime(directory)
            except OSError:
                continue
            stopped = False
        age = now - heartbeat
        if (stopped and age > PRUNE_STOPPED_AFTER_SECONDS) or age > PRUNE_SILENT_AFTER_SECONDS:
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(name)
    return removed


class AgentBridgeRuntime(object):
    """Owns the mailbox server, the request poller and the capture state for one qrenderdoc."""

    def __init__(self, renderdoc_version="", root=None, instance_id=None, environ=None,
                 handlers=None, poll_interval=POLL_INTERVAL_SECONDS, echo=None):
        self.root = os.path.abspath(root) if root else agent_root(environ)
        self.instance_id = instance_id or make_instance_id()
        self.renderdoc_version = renderdoc_version or ""
        self.bridge_version = BRIDGE_VERSION
        self.poll_interval = poll_interval
        self.state = CaptureState()
        self.handlers = dict(HANDLERS if handlers is None else handlers)
        self.methods = tuple(sorted(self.handlers))
        self.log = BridgeLog(os.path.join(self.root, "logs", "bridge-%s.log" % self.instance_id),
                             echo)
        self.server = MailboxServer(self.root, self.instance_id, self._handle,
                                    self.state.snapshot, methods=self.methods,
                                    renderdoc_version=self.renderdoc_version, log=self.log)
        self._poll_stop = threading.Event()
        self._poll_thread = None
        self.running = False

    def start(self):
        if self.running:
            return
        try:
            removed = prune_instances(self.root, self.instance_id)
            if removed:
                self.log("Pruned %d stale agent instance directories" % len(removed))
        except Exception as error:
            self.log("Pruning stale agent instances failed: %s" % error)
        self.server.start(start_worker=True)
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_main,
                                             name="RenderTestAgentBridgePoller")
        self._poll_thread.daemon = True
        self._poll_thread.start()
        self.running = True
        self.log("Agent bridge %s started for RenderDoc %s at %s" %
                 (self.instance_id, self.renderdoc_version, self.server.instance_dir))

    def stop(self, timeout=STOP_TIMEOUT_SECONDS):
        if not self.running:
            return True
        self._poll_stop.set()
        stopped = True
        if self._poll_thread is not None:
            self._poll_thread.join(timeout)
            if self._poll_thread.is_alive():
                stopped = False
            else:
                self._poll_thread = None
        if not self.server.stop(timeout):
            stopped = False
        self.running = not stopped
        self.log("Agent bridge %s %s" % (self.instance_id,
                                         "stopped" if stopped else "failed to stop cleanly"))
        return stopped

    def notify_state_changed(self):
        try:
            self.server.notify_activity()
        except Exception as error:
            self.log("Publishing agent manifest failed: %s" % error)

    def _poll_main(self):
        while not self._poll_stop.is_set():
            try:
                self.server.poll_once()
            except Exception as error:
                self.log("Agent bridge poll failed: %s" % error)
            self._poll_stop.wait(self.poll_interval)

    def _handle(self, context):
        handler = self.handlers.get(context.method)
        if handler is None:
            raise AgentBridgeError("UNKNOWN_METHOD",
                                   "The bridge does not implement %s yet." % context.method)
        return handler(self, context)
