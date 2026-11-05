"""qrendertest --ui-script helper for live bridge lifecycle checks.

Runs on the UI thread and steps through: wait for the capture -> select the first root event ->
close -> reload the same file -> done. Each step is appended to ``smoke-steps.log`` under
RENDERTEST_AGENT_ROOT so an external checker can line it up with the bridge manifest. The
script does not quit by itself: without PySide2 the main window widget is opaque to Python, so
the external driver (``live_lifecycle.py``) closes the window after it sees ``done``.

Environment:
    RENDERTEST_AGENT_SMOKE_CAPTURE   capture to reload after closing
    RENDERTEST_AGENT_SMOKE_STEP_MS   delay between steps, default 8000
"""

import os
import traceback

ctx = pyrenderdoc
capture = os.environ.get("RENDERTEST_AGENT_SMOKE_CAPTURE", "")
step_ms = int(os.environ.get("RENDERTEST_AGENT_SMOKE_STEP_MS", "8000"))
marker = os.path.join(os.environ.get("RENDERTEST_AGENT_ROOT", "."), "smoke-steps.log")


def note(text):
    with open(marker, "a") as stream:
        stream.write(text + "\n")


def step(function):
    def run():
        try:
            function()
        except Exception:
            note("error " + traceback.format_exc().replace("\n", " | "))
    return run


def when_loaded(then):
    def check():
        if ctx.IsCaptureLoaded():
            then()
        else:
            ctx.DelayedCallback(500, check)
    return step(check)


def loaded():
    note("loaded %d" % ctx.CurEvent())
    ctx.DelayedCallback(step_ms, step(select_event))


def select_event():
    actions = ctx.CurRootActions()
    target = actions[0].eventId if len(actions) > 0 else ctx.CurEvent()
    ctx.SetEventID([], target, target)
    note("selected %d" % target)
    ctx.DelayedCallback(step_ms, step(close_capture))


def close_capture():
    ctx.CloseCapture()
    note("closed")
    ctx.DelayedCallback(step_ms, step(reload_capture))


def reload_capture():
    note("reloading")
    ctx.LoadCapture(capture, ctx.Config().DefaultReplayOptions, capture, False, True)
    ctx.DelayedCallback(500, when_loaded(reloaded))


def reloaded():
    note("reloaded %d" % ctx.CurEvent())
    ctx.DelayedCallback(step_ms, step(finish))


def finish():
    note("done")


note("script started")
ctx.DelayedCallback(500, when_loaded(loaded))
