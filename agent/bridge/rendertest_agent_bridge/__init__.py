"""RenderTest built-in agent bridge.

qrendertest loads this package from ``<app>/integrations/rendertest_agent/python`` and calls
``start(version, ctx)`` after the normal extensions, then ``stop()`` before the capture context is
destroyed. Both entry points run on the Qt UI thread with the GIL held.
"""

__version__ = "0.1.0"

_runtime = None
_viewer = None
_ctx = None


def _print(message):
    try:
        print("[RenderTest Agent] %s" % message)
    except Exception:
        pass


def start(version, ctx):
    global _runtime, _viewer, _ctx
    if _runtime is not None:
        return

    import qrenderdoc as qrd

    from .lifecycle import make_capture_viewer
    from .runtime import AgentBridgeRuntime

    runtime = AgentBridgeRuntime(renderdoc_version=version)
    runtime.start()
    viewer = None
    try:
        viewer = make_capture_viewer(qrd, ctx, runtime.state, runtime.notify_state_changed)
        ctx.AddCaptureViewer(viewer)
    except Exception:
        if viewer is not None:
            try:
                ctx.RemoveCaptureViewer(viewer)
            except Exception:
                pass
        runtime.stop()
        raise

    _runtime = runtime
    _viewer = viewer
    _ctx = ctx
    _print("Bridge %s listening at %s" % (runtime.instance_id, runtime.server.instance_dir))


def stop():
    global _runtime, _viewer, _ctx
    viewer = _viewer
    if viewer is not None and _ctx is not None:
        try:
            _ctx.RemoveCaptureViewer(viewer)
        except Exception as error:
            _print("Removing capture viewer failed: %s" % error)
    _viewer = None

    runtime = _runtime
    if runtime is not None and not runtime.stop():
        raise RuntimeError("RenderTest agent bridge workers did not stop in time")
    _runtime = None
    _ctx = None
    if runtime is not None:
        _print("Bridge %s stopped" % runtime.instance_id)


def instance_id():
    return _runtime.instance_id if _runtime is not None else ""
