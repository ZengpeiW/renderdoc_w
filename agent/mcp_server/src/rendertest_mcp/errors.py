from __future__ import annotations

from typing import Any

from .protocol import ERROR_RETRYABLE


class RenderTestAgentError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_RETRYABLE:
            code = "INVALID_RESPONSE"
        super().__init__(message)
        self.code = code
        self.message = str(message)
        self.retryable = ERROR_RETRYABLE[code] if retryable is None else bool(retryable)
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
