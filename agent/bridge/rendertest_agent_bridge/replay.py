"""Run bridge work on the RenderDoc replay thread or the Qt UI thread.

The bridge worker never touches ``pyrenderdoc`` directly while serving replay requests: the
capture context wrapper marshals calls onto the UI thread, and the UI thread may itself be waiting
for the replay thread. Replay callbacks only use the ``IReplayController`` they are handed.
"""

import threading
import time
import traceback

from .ipc import AgentBridgeError

UI_WAIT_SLICE_SECONDS = 0.05


def _check_replay_health(controller):
    getter = getattr(controller, "GetFatalErrorStatus", None)
    if getter is None:
        return
    status = getter()
    try:
        failed = int(status.code) != 0
    except (AttributeError, TypeError, ValueError):
        failed = False
    if failed:
        raise AgentBridgeError("REPLAY_ERROR",
                               "The replay is in a fatal error state: %s" % status)


def _wrap_error(error, method, trace, log):
    if isinstance(error, AgentBridgeError):
        return error
    log("Replay work for %s failed:\n%s" % (method, trace))
    message = "%s: %s" % (type(error).__name__, error)
    return AgentBridgeError("REPLAY_ERROR", message[:1024])


class ReplayRunner(object):
    """Executes callables through ``IReplayManager.BlockInvoke`` for one bridge worker."""

    def __init__(self, state, log):
        self.state = state
        self.log = log
        self.manager = None

    def call(self, context, function):
        value, _ = self._invoke(context, function, None)
        return value

    def call_restoring(self, context, function):
        """Run work that may alter native replay state without changing its reported EID.

        FetchCounters is one such operation. It needs a forced replay of the latest UI event even
        if its numeric EID has not changed. Never invoke this work without a restorable UI event.
        """
        return self._invoke(context, function, None, force_restore=True)

    def at_event(self, context, event_id, function):
        """Run ``function(controller)`` with the replay moved to ``event_id``.

        Returns ``(value, meta)``. The replay is moved back to the UI's current event in the same
        callback, so foreground work queued behind this request never sees the temporary event.
        """
        return self._invoke(context, function, event_id)

    def _invoke(self, context, function, event_id, force_restore=False):
        manager = self.manager
        if manager is None:
            raise AgentBridgeError("UNSUPPORTED", "Replay access is not available.")
        context.check_cancelled()
        session = context.state.get("captureSessionId") or ""
        box = {"ran": False, "value": None, "error": None, "trace": "", "before": None,
               "restored": None, "restoreError": None}

        def invoke(controller):
            box["ran"] = True
            moved = False
            try:
                snapshot = self.state.snapshot()
                if (snapshot.get("captureSessionId") or "") != session:
                    raise AgentBridgeError("CAPTURE_CHANGED",
                                           "The capture changed before the request ran.")
                if time.time() > context.deadline_at:
                    raise AgentBridgeError("TIMEOUT", "The request expired in the replay queue.")
                box["before"] = snapshot.get("currentEventId")
                if force_restore:
                    if box["before"] is None:
                        raise AgentBridgeError("INVALID_EVENT", "The UI has no event to restore "
                                               "after replay work.")
                    moved = True
                if event_id is not None:
                    moved = True
                    controller.SetFrameEvent(event_id, False)
                _check_replay_health(controller)
                box["value"] = function(controller)
                if force_restore:
                    _check_replay_health(controller)
                    context.check_cancelled()
                    if (self.state.snapshot().get("captureSessionId") or "") != session:
                        raise AgentBridgeError("CAPTURE_CHANGED", "The capture changed during "
                                               "replay work.")
            except BaseException as error:
                box["error"] = error
                box["trace"] = traceback.format_exc()
            finally:
                if moved:
                    target = self.state.current_event_id()
                    if target is None:
                        target = box["before"]
                    try:
                        if target is not None:
                            controller.SetFrameEvent(target, force_restore)
                            if force_restore:
                                _check_replay_health(controller)
                        box["restored"] = target
                    except BaseException as error:
                        box["restoreError"] = "%s: %s" % (type(error).__name__, error)

        manager.BlockInvoke(invoke)
        if not box["ran"]:
            raise AgentBridgeError("CAPTURE_CHANGED",
                                   "The replay stopped before the request could run.")

        if (event_id is not None or (force_restore and box["restored"] is not None)) and box["restoreError"] is None:
            if force_restore:
                box["restored"] = self._reconcile(manager, box["restored"], force_restore=True)
            else:
                self._reconcile(manager, box["restored"])

        error = box["error"]
        if box["restoreError"] is not None:
            self.log("Restoring the replay event after %s failed: %s" %
                     (context.method, box["restoreError"]))
            if error is None:
                error = AgentBridgeError("REPLAY_ERROR",
                                         "Could not restore the replay event: %s" %
                                         box["restoreError"])
        if error is not None:
            raise _wrap_error(error, context.method, box["trace"], self.log)

        meta = {}
        if event_id is not None or force_restore:
            meta = {
                "replayEventId": event_id,
                "eventIdBefore": box["before"],
                "eventIdAfter": box["restored"],
                "replayEventRestored": box["restoreError"] is None,
            }
            if force_restore:
                meta["replayRestoreForced"] = True
        return box["value"], meta

    def _reconcile(self, manager, restored, force_restore=False):
        # qrenderdoc updates its current EID, queues SetFrameEvent and only notifies viewers once
        # that replay work finished. If this request ran inside that window it restored a stale
        # EID, so follow the UI to its newest event.
        latest = self.state.current_event_id()
        if latest is None or latest == restored:
            return restored

        status = {"ran": False, "error": None}

        def follow(controller):
            status["ran"] = True
            try:
                controller.SetFrameEvent(latest, force_restore)
                if force_restore:
                    _check_replay_health(controller)
            except BaseException as error:
                status["error"] = str(error)
                self.log("Following the UI event %s failed: %s" % (latest, error))

        manager.BlockInvoke(follow)
        if force_restore and (not status["ran"] or status["error"] is not None):
            raise AgentBridgeError("REPLAY_ERROR", "Could not force replay back to the latest "
                                   "UI event after replay work.")
        return latest


class UiInvoker(object):
    """Posts work onto the Qt UI thread without blocking the bridge worker."""

    def __init__(self, log):
        self.log = log
        self.helper = None
        self._closed = threading.Event()

    def close(self):
        self._closed.set()

    def call(self, context, function):
        helper = self.helper
        if helper is None:
            raise AgentBridgeError("UNSUPPORTED", "UI access is not available.")
        context.check_cancelled()
        done = threading.Event()
        box = {"value": None, "error": None, "trace": "", "abandoned": False}
        closed = self._closed

        def run():
            try:
                if closed.is_set() or box["abandoned"]:
                    return
                box["value"] = function()
            except BaseException as error:
                box["error"] = error
                box["trace"] = traceback.format_exc()
            finally:
                done.set()

        helper.InvokeOntoUIThread(run)
        try:
            while not done.wait(UI_WAIT_SLICE_SECONDS):
                context.check_cancelled()
        except AgentBridgeError:
            box["abandoned"] = True
            raise
        if box["error"] is not None:
            error = box["error"]
            if isinstance(error, AgentBridgeError):
                raise error
            self.log("UI work for %s failed:\n%s" % (context.method, box["trace"]))
            raise AgentBridgeError("INTERNAL_ERROR",
                                   ("%s: %s" % (type(error).__name__, error))[:1024])
        return box["value"]
