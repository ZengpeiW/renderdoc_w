"""Translate Claude SDK messages without running or emulating an agent loop."""
from __future__ import annotations

from typing import Any
import uuid

from .claude_backend import BackendError, LOGIN_MESSAGE, TOOL_NAMES


class TurnEvents:
    def __init__(self, backend: Any):
        self.backend = backend
        self.message_id = ""
        self.blocks: dict[tuple[str, int], dict[str, Any]] = {}
        self.completed: dict[str, dict[str, Any]] = {}
        self.has_text = False

    def complete(self, item: dict[str, Any]) -> None:
        rendered = self.render(item)
        if self.completed.get(item["id"]) != rendered:
            self.backend.item("item/completed", rendered)
            self.completed[item["id"]] = rendered

    def block(self, index: int, kind: str, text: str = "") -> dict[str, Any]:
        key = (self.message_id, index)
        if key not in self.blocks:
            item = {"id": self.backend.active_turn + ":" + self.message_id + ":" + str(index),
                    "type": "reasoning" if kind == "thinking" else "agentMessage", "text": text}
            self.blocks[key] = item
            self.backend.item("item/started", self.render(item))
        return self.blocks[key]

    @staticmethod
    def render(item: dict[str, Any]) -> dict[str, Any]:
        if item["type"] == "reasoning":
            return {"id": item["id"], "type": "reasoning", "summary": [item["text"]], "content": []}
        return dict(item)

    def validate_tool(self, name: str) -> None:
        if not self.backend.config.instance or name not in {"mcp__rendertest__" + tool for tool in TOOL_NAMES}:
            raise BackendError("Claude attempted a tool outside the RenderTest whitelist: " + name)

    async def consume(self, message: Any) -> bool:
        sdk = self.backend._load_sdk()
        session = getattr(message, "session_id", None)
        if session and session != self.backend.thread_id:
            raise BackendError("Claude emitted a different native session identity; refusing to continue.")
        if isinstance(message, sdk.StreamEvent):
            if message.parent_tool_use_id:
                raise BackendError("Unexpected delegated-agent output.")
            event = message.event
            kind = event.get("type")
            if kind == "message_start":
                self.message_id = event.get("message", {}).get("id") or uuid.uuid4().hex
            elif kind == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "tool_use":
                    self.validate_tool(block.get("name", ""))
                elif block.get("type") in ("text", "thinking"):
                    self.block(event["index"], block["type"], block.get("text", block.get("thinking", "")))
            elif kind == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") in ("text_delta", "thinking_delta"):
                    thinking = delta["type"] == "thinking_delta"
                    item = self.block(event["index"], "thinking" if thinking else "text")
                    text = delta.get("thinking" if thinking else "text", "")
                    item["text"] += text
                    self.backend.notify("item/reasoning/summaryTextDelta" if thinking else "item/agentMessage/delta",
                                        {"threadId": self.backend.thread_id, "turnId": self.backend.active_turn,
                                         "itemId": item["id"], "delta": text, "summaryIndex": 0})
                    self.has_text = self.has_text or not thinking
            elif kind == "content_block_stop":
                item = self.blocks.get((self.message_id, event.get("index")))
                if item:
                    self.complete(item)
        elif isinstance(message, sdk.AssistantMessage):
            if message.parent_tool_use_id:
                raise BackendError("Unexpected delegated-agent output.")
            if message.error:
                raise BackendError(LOGIN_MESSAGE if message.error == "authentication_failed" else "Claude error: " + message.error,
                                   -32001 if message.error == "authentication_failed" else -32000)
            self.message_id = message.message_id or self.message_id or uuid.uuid4().hex
            for index, block in enumerate(message.content):
                if isinstance(block, sdk.ToolUseBlock):
                    self.validate_tool(block.name)
                elif isinstance(block, (sdk.TextBlock, sdk.ThinkingBlock)):
                    thinking = isinstance(block, sdk.ThinkingBlock)
                    item = self.block(index, "thinking" if thinking else "text")
                    item["text"] = block.thinking if thinking else block.text
                    self.complete(item)
                    self.has_text = self.has_text or not thinking
        elif isinstance(message, sdk.ResultMessage):
            if message.result and not self.has_text and not message.is_error:
                self.message_id = "result"
                item = self.block(0, "text", message.result)
                self.complete(item)
            usage = message.usage or {}
            tokens = sum(int(usage.get(key, 0)) for key in
                         ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if self.backend.total_tokens is not None:
                self.backend.total_tokens += tokens
                self.backend.notify("thread/tokenUsage/updated", {"threadId": self.backend.thread_id,
                                    "turnId": self.backend.active_turn,
                                    "tokenUsage": {"total": {"totalTokens": self.backend.total_tokens},
                                                   "last": {"totalTokens": tokens}}})
            return True
        elif isinstance(message, sdk.SystemMessage):
            if message.subtype == "init":
                native_id = message.data.get("session_id")
                if native_id and native_id != self.backend.thread_id:
                    raise BackendError("Claude initialized a different native session.")
                for name in message.data.get("tools", []):
                    self.validate_tool(name)
        return False
