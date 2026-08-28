"""Small helpers shared by the resolvers."""

from __future__ import annotations

import re
from typing import Any

from ..errors import InvalidIntentError

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase snake_case form used for lenient name matching."""
    return "_".join(_TOKEN_RE.findall(text.lower()))


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def slugify(text: str) -> str:
    slug = normalize(text)
    if not slug:
        raise InvalidIntentError(f"Cannot derive a valid name from {text!r}.", field="name")
    if slug[0].isdigit():
        slug = f"d_{slug}"
    return slug


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
