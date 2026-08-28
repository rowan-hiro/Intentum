"""Access-control hook.

The backend consults an ``AccessPolicy`` before every operation. The default
allows everything; deployments can plug in a real policy without touching
the semantic operations.
"""

from __future__ import annotations

from typing import Any, Protocol

from .errors import PermissionDeniedError


class AccessPolicy(Protocol):
    def check(self, principal: str | None, action: str, resource: dict[str, Any]) -> None:
        """Raise ``PermissionDeniedError`` to deny."""


class AllowAllPolicy:
    def check(self, principal: str | None, action: str, resource: dict[str, Any]) -> None:
        return None


class DenyActionsPolicy:
    """Small policy useful for tests and demos: deny a set of actions."""

    def __init__(self, denied: set[str], principals: set[str] | None = None) -> None:
        self.denied = denied
        self.principals = principals

    def check(self, principal: str | None, action: str, resource: dict[str, Any]) -> None:
        if action in self.denied and (self.principals is None or principal in self.principals):
            raise PermissionDeniedError(
                f"Principal {principal!r} is not allowed to {action}.",
                details={"action": action, "resource": resource},
            )
