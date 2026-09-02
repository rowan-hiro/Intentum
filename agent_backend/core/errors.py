"""Structured error model.

Every failure that can reach an agent is a ``BackendError`` carrying a stable
``code`` and a ``recoverable`` flag, so the agent can repair its intent instead
of parsing exception strings.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_TRANSFORM = "INVALID_TRANSFORM"
    INVALID_INTENT = "INVALID_INTENT"
    INVALID_STATE = "INVALID_STATE"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    CONFLICT = "CONFLICT"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    INTERNAL = "INTERNAL"


class BackendError(Exception):
    """Base class for all agent-visible failures."""

    code: ErrorCode = ErrorCode.INTERNAL
    recoverable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        recoverable: bool | None = None,
        field: str | None = None,
        candidates: list[Any] | None = None,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if recoverable is not None:
            self.recoverable = recoverable
        self.field = field
        self.candidates = candidates or []
        self.details = details or {}
        self.hint = hint
        # Teach on refusal (MADR 0010): what the backend accepts instead, attached by core/recovery.
        self.advice: list[Any] = []

    def to_response(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": "error",
            "code": str(self.code),
            "message": self.message,
            "recoverable": self.recoverable,
        }
        if self.field:
            body["field"] = self.field
        if self.candidates:
            body["candidates"] = self.candidates
        if self.details:
            body["details"] = self.details
        if self.hint:
            body["hint"] = self.hint
        if self.advice:
            body["advice"] = [a.to_dict() if hasattr(a, "to_dict") else a for a in self.advice]
        return body


class NotFoundError(BackendError):
    code = ErrorCode.NOT_FOUND
    recoverable = True


class AmbiguousReferenceError(BackendError):
    """Material ambiguity: the backend refuses to guess.

    Rendered as a ``needs_resolution`` response with candidates.
    """

    code = ErrorCode.AMBIGUOUS_REFERENCE
    recoverable = True

    def to_response(self) -> dict[str, Any]:
        body = super().to_response()
        body["status"] = "needs_resolution"
        return body


class InvalidSchemaError(BackendError):
    code = ErrorCode.INVALID_SCHEMA
    recoverable = True


class InvalidTransformError(BackendError):
    code = ErrorCode.INVALID_TRANSFORM
    recoverable = True


class InvalidIntentError(BackendError):
    code = ErrorCode.INVALID_INTENT
    recoverable = True


class InvalidStateError(BackendError):
    code = ErrorCode.INVALID_STATE
    recoverable = True


class TypeMismatchError(BackendError):
    code = ErrorCode.TYPE_MISMATCH
    recoverable = True


class ConflictError(BackendError):
    code = ErrorCode.CONFLICT
    recoverable = True


class ContractMismatchError(BackendError):
    """The result does not have the shape the agent declared for it (MADR 0007).

    Recoverable in two ways, both spelled out in the hint: reshape the dataset,
    or amend the contract with a reason.
    """

    code = ErrorCode.CONTRACT_MISMATCH
    recoverable = True


class PermissionDeniedError(BackendError):
    code = ErrorCode.PERMISSION_DENIED
    recoverable = False


class ExecutionFailedError(BackendError):
    code = ErrorCode.EXECUTION_FAILED
    recoverable = False
