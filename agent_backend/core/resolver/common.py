"""Small helpers shared by the resolvers."""

from __future__ import annotations

from typing import Any

from ..errors import InvalidIntentError
from ..naming import is_cjk, is_identifier, normalize, slugify, tokens

__all__ = [
    "ResolutionNote",
    "is_cjk",
    "is_identifier",
    "normalize",
    "pick",
    "reject_unknown_keys",
    "slugify",
    "tokens",
]


def pick(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def reject_unknown_keys(mapping: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(k for k in mapping if k not in allowed)
    if unknown:
        raise InvalidIntentError(
            f"Unknown key(s) {unknown} in {where}.",
            field=where,
            details={"allowed_keys": sorted(allowed)},
        )


class ResolutionNote:
    """A human-readable record of a non-trivial resolution decision.

    Notes are returned to the agent so that every lenient match is visible
    rather than silent.
    """

    def __init__(self, field: str, reference: str, resolved_to: str, reason: str) -> None:
        self.field = field
        self.reference = reference
        self.resolved_to = resolved_to
        self.reason = reason

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "reference": self.reference,
            "resolved_to": self.resolved_to,
            "reason": self.reason,
        }
