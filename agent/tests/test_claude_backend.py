"""Contract tests use the real SDK types/options with an injected, non-network SDK client."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

sdk_types = pytest.importorskip("claude_agent_sdk")

from mcp.types import CallToolRequest, CallToolRequestParams, TextContent, Tool
from rendertest_mcp.claude_backend import (BackendConfig, BackendError, CancelableBridgeClient,
                                          ClaudeBackend, EFFORTS, POLICY, SKILLS)
from rendertest_mcp.instances import read_instance
from rendertest_mcp.paths import instances_root
from rendertest_mcp.protocol import INSTANCE_SCHEMA, PROTOCOL, RESULT_SCHEMA, TOOL_NAMES


class FakeSDK:
    def __init__(self):
        self.clients = []
        self.persisted = set()
        self.queries = []
        self.script = None
        self.connect_gate = None
        self.connect_started = None
        self.status = None
        self.account = {"apiKeySource": "environment", "tokenSource": "none"}

    def __getattr__(self, name):
        return getattr(sdk_types, name)

    def get_session_info(self, session_id, directory=None):
        return SimpleNamespace(session_id=session_id) if session_id in self.persisted else None

    def ClaudeSDKClient(self, options):
        client = FakeClient(self, options)
        self.clients.append(client)
        return client


class FakeClient:
    def __init__(self, sdk, options):
        self.sdk, self.options = sdk, options
        self.session_id = options.resume or options.session_id or str(uuid.uuid4())
        self.owner = None
        self.closed = False
        self.stopped = asyncio.Event()

    async def connect(self):
        self.owner = asyncio.current_task()
        if self.sdk.connect_started:
            self.sdk.connect_started.set()
        if self.sdk.connect_gate:
            await self.sdk.connect_gate.wait()
        if self.options.resume and self.options.resume not in self.sdk.persisted:
            raise RuntimeError("native session missing")

    async def disconnect(self):
        assert asyncio.current_task() is self.owner
        self.closed = True

    async def get_server_info(self):
        return {"current_permission_mode": "dontAsk", "account": self.sdk.account, "models": [{"value": "sonnet", "displayName": "Sonnet",
                "description": "test model", "supportedEffortLevels": list(EFFORTS)}]}

    async def get_mcp_status(self):
        if self.sdk.status is not None:
            return self.sdk.status
        return {"mcpServers": [{"name": "rendertest", "status": "connected",
                "tools": [{"name": name} for name in TOOL_NAMES]}] if self.options.mcp_servers else []}

    async def query(self, text, session_id="default"):
        assert asyncio.current_task() is self.owner
        assert session_id == self.session_id
        self.sdk.queries.append((session_id, text))
        self.sdk.persisted.add(session_id)

    async def interrupt(self):
        self.stopped.set()

    async def receive_response(self):
        if self.sdk.script:
            async for item in self.sdk.script(self):
                yield item
            return
        yield sdk_types.AssistantMessage(content=[sdk_types.TextBlock(text="answer")], model="sonnet",
                                         session_id=self.session_id, message_id="message")
        yield result(self.session_id)


def result(session_id, **kwargs):
    values = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                  num_turns=1, session_id=session_id, usage={"input_tokens": 5, "output_tokens": 2})
    values.update(kwargs)
    return sdk_types.ResultMessage(**values)


def backend(tmp_path, sdk=None, **config):
    sent = []
    sdk = sdk or FakeSDK()
    return ClaudeBackend(BackendConfig(tmp_path, **config), sent.append, sdk=sdk), sdk, sent


async def start(bridge, **params):
    await bridge.handle("initialize", {})
    response = await bridge.handle("thread/start", dict(sandbox="read-only", approvalPolicy="never", **params))
    return response["thread"]["id"]


async def turn(bridge, text="question", **params):
    reply = await bridge.handle("turn/start", {"threadId": bridge.thread_id,
                                "input": [{"type": "text", "text": text}], **params})
    await bridge.turn_task
    return reply


def bind_capture(root: Path, *, filename: str = "capture.rdc"):
    instance, session = "test-instance", uuid.uuid4().hex
    capture = root / filename
    capture.write_bytes(b"local capture fixture")
    directory = instances_root(root) / instance
    directory.mkdir(parents=True)
    manifest = {"schema": INSTANCE_SCHEMA, "protocol": PROTOCOL, "instanceId": instance,
                "pid": 123, "heartbeatUnixMs": int(time.time() * 1000),
                "lastActivityUnixMs": int(time.time() * 1000), "captureLoaded": True,
                "captureSessionId": session, "capturePath": str(capture), "stopped": False}
    (directory / "instance.json").write_text(json.dumps(manifest), encoding="utf-8")
    return instance, session, directory, manifest


def test_metadata_concurrent_calls_have_one_sdk_owner_and_no_query(tmp_path):
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        await bridge.handle("initialize", {})
        account, models = await asyncio.gather(bridge.handle("account/read", {}), bridge.handle("model/list", {}))
        assert len(sdk.clients) == 1 and not sdk.queries
        assert account["requiresAuth"] is False and account["browserLoginSupported"] is False
        assert models["data"][0]["model"] == "sonnet"
        assert [x["reasoningEffort"] for x in models["data"][0]["supportedReasoningEfforts"]] == list(EFFORTS)
        await bridge.close()
        assert sdk.clients[0].closed
    asyncio.run(check())


def test_policy_options_disable_builtins_settings_skills_and_foreign_mcp(tmp_path):
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        response = await bridge.handle("initialize", {})
        assert response["backend"] == "claude"
        response = await bridge.handle("thread/start", {"sandbox": "read-only", "approvalPolicy": "never"})
        assert response["policy"] == POLICY and "sandbox" not in response
        options = sdk.clients[-1].options
        assert options.tools == options.skills == options.setting_sources == options.plugins == []
        assert options.hooks == {} and options.mcp_servers == {} and options.strict_mcp_config
        assert options.permission_mode == "dontAsk" and options.allowed_tools == []
        assert json.loads(options.settings)["disableAllHooks"] is True
        assert isinstance(await options.can_use_tool("Bash", {}, None), sdk_types.PermissionResultDeny)
        status = await bridge.handle("mcpServerStatus/list", {"threadId": bridge.thread_id})
        assert status["data"][0]["runtimeStatus"] == "disabled"
        await bridge.close()
    asyncio.run(check())


def test_auth_is_external_and_does_not_claim_missing_credentials(tmp_path):
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        sdk.account = {"apiKeySource": "none", "tokenSource": "none"}
        await bridge.handle("initialize", {})
        account = await bridge.handle("account/read", {})
        assert account["requiresAuth"] and account["account"] is None
        assert "outside RenderTest" in account["loginMessage"]
        with pytest.raises(BackendError, match="outside RenderTest"):
            await bridge.handle("account/login/start", {})
        await bridge.close()
    asyncio.run(check())


def test_missing_dependency_message_is_actionable(tmp_path, monkeypatch):
    async def check():
        bridge = ClaudeBackend(BackendConfig(tmp_path), lambda _: None)
        def missing(_):
            raise ImportError("missing SDK")
        monkeypatch.setattr("rendertest_mcp.claude_backend.importlib.import_module", missing)
        with pytest.raises(BackendError, match=r"\[claude\]"):
            await bridge.handle("initialize", {})
        await bridge.close()
    asyncio.run(check())


def test_stream_and_final_assistant_completion_are_idempotent(tmp_path):
    async def script(client):
        sid = client.session_id
        for event in ({"type": "message_start", "message": {"id": "native-message"}},
                      {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
                      {"type": "content_block_stop", "index": 0}):
            yield sdk_types.StreamEvent(uuid=uuid.uuid4().hex, session_id=sid, event=event)
        yield sdk_types.AssistantMessage(content=[sdk_types.TextBlock(text="hello")], model="sonnet",
                                         session_id=sid, message_id="native-message")
        yield result(sid, result="hello")
    async def check():
        bridge, sdk, sent = backend(tmp_path)
        sdk.script = script
        await start(bridge)
        await turn(bridge)
        agents = [item["params"]["item"] for item in sent if item["method"] == "item/completed"]
        assert len(agents) == 1 and agents[0]["text"] == "hello"
        assert [item["params"]["delta"] for item in sent if item["method"] == "item/agentMessage/delta"] == ["hello"]
        assert sent[-1]["params"]["turn"]["status"] == "completed"
        await bridge.close()
    asyncio.run(check())


def test_resume_requires_native_session_and_restores_token_total(tmp_path):
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        thread_id = await start(bridge)
        await turn(bridge)
        await bridge.close()
        resumed, _, sent = backend(tmp_path, sdk)
        await resumed.handle("initialize", {})
        reply = await resumed.handle("thread/resume", {"threadId": thread_id, "sandbox": "read-only",
                                      "approvalPolicy": "never", "excludeTurns": True})
        assert reply["thread"]["id"] == thread_id and reply["thread"]["turns"] == []
        assert sdk.clients[-1].options.resume == thread_id and sdk.clients[-1].options.session_id is None
        await turn(resumed)
        totals = [entry["params"]["tokenUsage"]["total"]["totalTokens"] for entry in sent
                  if entry["method"] == "thread/tokenUsage/updated"]
        assert totals == [14]
        await resumed.close()
        sdk.persisted.clear()
        missing, _, _ = backend(tmp_path, sdk)
        await missing.handle("initialize", {})
        with pytest.raises(BackendError, match="native Claude session"):
            await missing.handle("thread/resume", {"threadId": thread_id, "sandbox": "read-only", "approvalPolicy": "never"})
        assert missing.client is None
        await missing.close()
    asyncio.run(check())


def test_missing_or_foreign_resume_never_falls_back_to_new_session(tmp_path):
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        await bridge.handle("initialize", {})
        with pytest.raises(BackendError, match="unavailable"):
            await bridge.handle("thread/resume", {"threadId": str(uuid.uuid4()), "sandbox": "read-only", "approvalPolicy": "never"})
        assert not sdk.clients
        with pytest.raises(BackendError, match="canonical UUID|Invalid Claude"):
            await bridge.handle("thread/resume", {"threadId": "../../other", "sandbox": "read-only", "approvalPolicy": "never"})
        await bridge.close()
    asyncio.run(check())


def test_prepared_session_continues_with_same_uuid_without_claiming_native_resume(tmp_path):
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        thread_id = await start(bridge)
        assert bridge.ledger["querySubmitted"] is False and not sdk.persisted
        await bridge.close()
        prepared, _, _ = backend(tmp_path, sdk)
        await prepared.handle("initialize", {})
        response = await prepared.handle("thread/resume", {"threadId": thread_id, "sandbox": "read-only",
                                         "approvalPolicy": "never"})
        assert response["resumeState"] == "prepared" and response["thread"]["id"] == thread_id
        assert sdk.clients[-1].options.session_id == thread_id and sdk.clients[-1].options.resume is None
        await turn(prepared)
        assert prepared.ledger["querySubmitted"] is True and sdk.queries[0][0] == thread_id
        await prepared.close()
    asyncio.run(check())


@pytest.mark.parametrize("submitted", [True, None])
def test_attempted_or_legacy_session_without_native_history_is_rejected(tmp_path, submitted):
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        thread_id = await start(bridge)
        if submitted is None:
            bridge.ledger.pop("querySubmitted")
        else:
            bridge.ledger["querySubmitted"] = submitted
        bridge._save_ledger()
        await bridge.close()
        restored, _, _ = backend(tmp_path, sdk)
        await restored.handle("initialize", {})
        with pytest.raises(BackendError, match="native Claude session is missing"):
            await restored.handle("thread/resume", {"threadId": thread_id, "sandbox": "read-only", "approvalPolicy": "never"})
        assert restored.client is None
        await restored.close()
    asyncio.run(check())


class FakeToolServer:
    def __init__(self, envelope):
        self.envelope = envelope
        self.calls = []

    async def list_tools(self):
        return [Tool(name=name, inputSchema={"type": "object", "properties": {}}) for name in TOOL_NAMES]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return [TextContent(type="text", text="preview")], self.envelope


def test_inprocess_mcp_preserves_structured_artifacts_and_rechecks_binding(tmp_path):
    async def check():
        instance, session, directory, manifest = bind_capture(tmp_path)
        envelope = {"schema": RESULT_SCHEMA, "instanceId": instance, "captureSessionId": session,
                    "meta": {"captureSessionId": session}, "data": {"large": "x" * 6000},
                    "artifacts": [{"path": str(tmp_path / "image.png"), "mediaType": "image/png", "bytes": 1,
                                   "sha256": "a" * 64}]}
        tool_server = FakeToolServer(envelope)
        sdk = FakeSDK()
        sent = []
        bridge = ClaudeBackend(BackendConfig(tmp_path, instance=instance, capture_session=session),
                               sent.append, sdk=sdk, tool_server=tool_server)
        await start(bridge)
        bridge.active_turn = str(uuid.uuid4())
        server = sdk.clients[-1].options.mcp_servers["rendertest"]["instance"]
        request = CallToolRequest(params=CallToolRequestParams(name="get_context", arguments={}))
        result_object = await server.request_handlers[CallToolRequest](request)
        assert result_object.root.structuredContent == envelope
        completed = [entry["params"]["item"] for entry in sent if entry["method"] == "item/completed"]
        assert completed[-1]["result"]["structuredContent"] == envelope
        manifest["captureSessionId"] = uuid.uuid4().hex
        (directory / "instance.json").write_text(json.dumps(manifest))
        result_object = await server.request_handlers[CallToolRequest](request)
        assert result_object.root.isError and len(tool_server.calls) == 1
        bridge.active_turn = ""
        await bridge.close()
    asyncio.run(check())


def test_capture_resume_rejects_different_file_content(tmp_path):
    async def check():
        instance, session, _, manifest = bind_capture(tmp_path)
        bridge, sdk, _ = backend(tmp_path, instance=instance, capture_session=session)
        thread_id = await start(bridge)
        await turn(bridge)
        await bridge.close()
        Path(manifest["capturePath"]).write_bytes(b"changed capture")
        restored, _, _ = backend(tmp_path, sdk, instance=instance, capture_session=session)
        await restored.handle("initialize", {})
        with pytest.raises(BackendError, match="capture identity"):
            await restored.handle("thread/resume", {"threadId": thread_id, "sandbox": "read-only", "approvalPolicy": "never"})
        await restored.close()
    asyncio.run(check())


def test_remote_capture_can_start_but_cannot_resume(tmp_path):
    async def check():
        instance, session, directory, manifest = bind_capture(tmp_path)
        manifest["capturePath"] = "remote://capture"
        (directory / "instance.json").write_text(json.dumps(manifest))
        bridge, sdk, _ = backend(tmp_path, instance=instance, capture_session=session)
        thread_id = await start(bridge)
        await turn(bridge)
        await bridge.close()
        restored, _, _ = backend(tmp_path, sdk, instance=instance, capture_session=session)
        await restored.handle("initialize", {})
        with pytest.raises(BackendError, match="remote or unsaved"):
            await restored.handle("thread/resume", {"threadId": thread_id, "sandbox": "read-only", "approvalPolicy": "never"})
        await restored.close()
    asyncio.run(check())


def test_foreign_mcp_runtime_is_rejected(tmp_path):
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        await start(bridge)
        sdk.status = {"mcpServers": [{"name": "foreign", "status": "connected"}]}
        with pytest.raises(BackendError, match="unexpected MCP"):
            await bridge.handle("mcpServerStatus/list", {"threadId": bridge.thread_id})
        await bridge.close()
    asyncio.run(check())


def test_initial_empty_mcp_status_is_bounded_starting_but_disappearance_fails(tmp_path):
    async def check():
        instance, session, _, _ = bind_capture(tmp_path)
        bridge, sdk, _ = backend(tmp_path, instance=instance, capture_session=session)
        await start(bridge)
        sdk.status = {"mcpServers": []}
        status = await bridge.handle("mcpServerStatus/list", {"threadId": bridge.thread_id})
        assert status["data"][0]["runtimeStatus"] == "starting" and not status["data"][0]["tools"]
        bridge.connected_at -= 21
        with pytest.raises(BackendError, match="missing"):
            await bridge.handle("mcpServerStatus/list", {"threadId": bridge.thread_id})
        bridge.connected_at = time.monotonic()
        sdk.status = None
        assert (await bridge.handle("mcpServerStatus/list", {"threadId": bridge.thread_id}))["data"][0]["runtimeStatus"] == "connected"
        sdk.status = {"mcpServers": []}
        with pytest.raises(BackendError, match="missing"):
            await bridge.handle("mcpServerStatus/list", {"threadId": bridge.thread_id})
        await bridge.close()
    asyncio.run(check())


def test_unapproved_builtin_output_fails_turn_and_disconnects(tmp_path):
    async def script(client):
        yield sdk_types.AssistantMessage(content=[sdk_types.ToolUseBlock(id="tool", name="Bash", input={})], model="sonnet")
    async def check():
        bridge, sdk, sent = backend(tmp_path)
        sdk.script = script
        await start(bridge)
        await turn(bridge)
        assert sent[-2]["method"] == "turn/completed" and sent[-2]["params"]["turn"]["status"] == "failed"
        assert sent[-1]["method"] == "backend/disconnected" and bridge.client is None
        with pytest.raises(BackendError, match="idle Claude thread"):
            await bridge.handle("turn/start", {"threadId": bridge.thread_id, "input": []})
        await bridge.close()
    asyncio.run(check())


def test_stop_during_effort_reconnect_never_queries_model(tmp_path):
    async def check():
        bridge, sdk, sent = backend(tmp_path)
        await start(bridge)
        sdk.connect_gate, sdk.connect_started = asyncio.Event(), asyncio.Event()
        response = await bridge.handle("turn/start", {"threadId": bridge.thread_id, "effort": "high",
                                       "input": [{"type": "text", "text": "must not send"}]})
        await sdk.connect_started.wait()
        await bridge.handle("turn/interrupt", {"threadId": bridge.thread_id, "turnId": response["turn"]["id"]})
        sdk.connect_gate.set()
        await bridge.turn_task
        assert not sdk.queries
        assert sent[-1]["params"]["turn"]["status"] == "interrupted"
        await bridge.close()
        assert all(client.closed for client in sdk.clients)
    asyncio.run(check())


def test_eof_interrupts_active_sdk_turn_and_disconnects_owner(tmp_path):
    async def script(client):
        await client.stopped.wait()
        yield result(client.session_id, terminal_reason="aborted_streaming")
    async def check():
        bridge, sdk, _ = backend(tmp_path)
        sdk.script = script
        await start(bridge)
        await bridge.handle("turn/start", {"threadId": bridge.thread_id, "input": []})
        await asyncio.sleep(0)
        await bridge.close()
        assert all(client.closed for client in sdk.clients)
        assert bridge.owner.task.done()
    asyncio.run(check())


def test_mailbox_cancellation_is_prompt_and_removes_unclaimed_request(tmp_path):
    instance, session, directory, _ = bind_capture(tmp_path)
    cancelled = threading.Event()
    client = CancelableBridgeClient(root=tmp_path, cancelled=cancelled)
    request = directory / "request.json"
    request.write_text("{}")
    cancelled.set()
    with pytest.raises(Exception, match="interrupted"):
        client._wait_for_response(read_instance(directory), "id", request, directory / "response.json", time.time() + 120)
    assert not request.exists()


def test_only_startup_skills_are_accepted_and_injected(tmp_path):
    async def check():
        root = tmp_path / "skills"
        for name in SKILLS:
            path = root / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("Trusted body for " + name)
        bridge, sdk, _ = backend(tmp_path / "agent", skills_roots=[root])
        await bridge.handle("initialize", {})
        await bridge.handle("skills/extraRoots/set", {"extraRoots": [str(root)]})
        with pytest.raises(BackendError, match="startup-configured"):
            await bridge.handle("skills/extraRoots/set", {"extraRoots": [str(tmp_path)]})
        await bridge.handle("thread/start", {"sandbox": "read-only", "approvalPolicy": "never"})
        assert all("Trusted body for " + name in sdk.clients[-1].options.system_prompt for name in SKILLS)
        assert sdk.clients[-1].options.skills == []
        await bridge.close()
    asyncio.run(check())
