"""Structured logging for the request pipeline.

Each pipeline stage emits one structured event (``stage`` plus key/value
fields) so a failed agent request can be traced from received intent through
resolution, canonical IR, validation, plan, execution and commit.
"""

from __future__ import annotations

import json
import logging
from typing import Any

LOGGER_NAME = "agent_backend"
logger = logging.getLogger(LOGGER_NAME)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def log_event(stage: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured pipeline event.

    The message is ``stage=<stage>`` followed by a JSON payload; the raw
    fields are attached as ``record.event`` for structured handlers.
    """
    payload = {k: _jsonable(v) for k, v in fields.items()}
    logger.log(level, "stage=%s %s", stage, json.dumps(payload, default=str), extra={"event": {"stage": stage, **payload}})


class EventCapture(logging.Handler):
    """Test/inspection helper that keeps structured events in memory."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        event = getattr(record, "event", None)
        if event is not None:
            self.events.append(event)

    def stages(self) -> list[str]:
        return [e["stage"] for e in self.events]
