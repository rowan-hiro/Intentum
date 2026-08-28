"""Agent-ready backend: a semantic, deterministic execution substrate."""

from .core.backend import Backend
from .core.errors import BackendError, ErrorCode

__all__ = ["Backend", "BackendError", "ErrorCode"]
