import os


def agent_root(environ=None):
    environ = os.environ if environ is None else environ
    override = (environ.get("RENDERTEST_AGENT_ROOT") or "").strip()
    if override:
        return os.path.abspath(override)

    local_root = (environ.get("LOCALAPPDATA") or environ.get("XDG_DATA_HOME") or "").strip()
    if not local_root:
        local_root = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(os.path.abspath(local_root), "RenderTest", "agent")


def instances_root(root):
    return os.path.join(os.path.abspath(root), "ipc-v1", "instances")


def artifacts_root(root):
    return os.path.join(os.path.abspath(root), "artifacts")
