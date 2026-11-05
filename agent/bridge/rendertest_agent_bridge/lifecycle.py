import threading
import uuid


def _enum_name(value):
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(value).split(".")[-1]


def _event_id(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class CaptureState(object):
    """Thread-safe snapshot of the qrenderdoc capture state.

    All update methods run on the Qt UI thread from CaptureViewer callbacks. The IPC worker only
    reads snapshots, so it never needs to call back into qrenderdoc for status queries.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._clear()

    def _clear(self):
        self._loaded = False
        self._session_id = ""
        self._path = ""
        self._api = ""
        self._selected_event_id = None
        self._current_event_id = None
        self._selected_action = None
        self._current_action = None

    def snapshot(self):
        with self._lock:
            return {
                "captureLoaded": self._loaded,
                "capturePath": self._path,
                "captureSessionId": self._session_id,
                "api": self._api,
                "selectedEventId": self._selected_event_id,
                "currentEventId": self._current_event_id,
                "selectedAction": dict(self._selected_action) if self._selected_action else None,
                "currentAction": dict(self._current_action) if self._current_action else None,
            }

    def current_event_id(self):
        with self._lock:
            return self._current_event_id if self._loaded else None

    def capture_loaded(self, path, api, selected=None, current=None):
        with self._lock:
            self._clear()
            self._loaded = True
            self._session_id = uuid.uuid4().hex
            self._path = path or ""
            self._api = api or ""
            if selected is not None:
                self._selected_event_id, self._selected_action = selected
            if current is not None:
                self._current_event_id, self._current_action = current

    def capture_closed(self):
        with self._lock:
            self._clear()

    def selected_event_changed(self, event_id, action):
        with self._lock:
            if not self._loaded:
                return
            self._selected_event_id = _event_id(event_id)
            self._selected_action = action

    def current_event_changed(self, event_id, action):
        with self._lock:
            if not self._loaded:
                return
            self._current_event_id = _event_id(event_id)
            self._current_action = action


def describe_action(ctx, event_id):
    """Summarise an action on the UI thread. Returns None when it is unavailable."""
    event_id = _event_id(event_id)
    if event_id is None:
        return None
    try:
        action = ctx.GetAction(event_id)
    except Exception:
        return None
    if action is None:
        return None
    name = ""
    try:
        name = action.GetName(ctx.GetStructuredFile())
    except Exception:
        name = getattr(action, "customName", "") or ""
    flags = getattr(action, "flags", None)
    summary = {
        "eventId": int(action.eventId),
        "actionId": int(getattr(action, "actionId", 0)),
        "name": str(name),
        "flags": str(flags) if flags is not None else "",
    }
    try:
        summary["flagsValue"] = int(flags)
    except (TypeError, ValueError):
        pass
    return summary


def _event_with_action(ctx, getter):
    try:
        event_id = _event_id(getter())
    except Exception:
        return None
    return event_id, describe_action(ctx, event_id)


def make_capture_viewer(qrd, ctx, state, on_change):
    """Build a qrenderdoc CaptureViewer that mirrors the UI capture state into ``state``."""

    class AgentCaptureViewer(qrd.CaptureViewer):
        def __init__(self):
            qrd.CaptureViewer.__init__(self)

        def OnCaptureLoaded(self):
            path = ""
            api = ""
            try:
                path = ctx.GetCaptureFilename()
            except Exception:
                pass
            try:
                api = _enum_name(ctx.APIProps().pipelineType)
            except Exception:
                pass
            # qrenderdoc selects the initial event before notifying viewers and excludes them from
            # that event notification, so the current events must be read here.
            state.capture_loaded(path, api,
                                 selected=_event_with_action(ctx, ctx.CurSelectedEvent),
                                 current=_event_with_action(ctx, ctx.CurEvent))
            on_change()

        def OnCaptureClosed(self):
            state.capture_closed()
            on_change()

        def OnSelectedEventChanged(self, event_id):
            state.selected_event_changed(event_id, describe_action(ctx, event_id))
            on_change()

        def OnEventChanged(self, event_id):
            state.current_event_changed(event_id, describe_action(ctx, event_id))
            on_change()

    return AgentCaptureViewer()
