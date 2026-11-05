"""Live smoke test for the built-in bridge inside a running qrendertest.

Usage:
    python devtools/smoke_live.py [--root DIR] [--instance-id ID] [--expect-capture] [--wait 30]

Waits for an online instance, then checks ping/get_status/get_context and prints the results.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = AGENT_DIR / "mcp_server" / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from rendertest_mcp.errors import RenderTestAgentError  # noqa: E402
from rendertest_mcp.ipc_client import BridgeClient  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test a live RenderTest agent bridge.")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--expect-capture", action="store_true")
    parser.add_argument("--wait", type=float, default=30.0)
    args = parser.parse_args(argv)

    client = BridgeClient(root=args.root)
    deadline = time.monotonic() + args.wait
    instances: list[dict] = []
    while time.monotonic() < deadline:
        instances = [
            item
            for item in client.list_instances()
            if args.instance_id is None or item["instanceId"] == args.instance_id
        ]
        if instances and (not args.expect_capture or instances[0].get("captureLoaded")):
            break
        time.sleep(0.5)
    else:
        print(f"No matching online instance under {client.root} within {args.wait}s", file=sys.stderr)
        return 1

    instance_id = instances[0]["instanceId"]
    print("instance:", json.dumps(instances[0], ensure_ascii=False))
    try:
        for method in ("ping", "get_status", "get_context"):
            response = client.call(method, instance_id=instance_id)
            print(f"{method}:", json.dumps(response.result["data"], ensure_ascii=False))
    except RenderTestAgentError as error:
        print(f"bridge error: {error.code}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
