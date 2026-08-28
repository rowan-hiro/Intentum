"""Audit trail for every state-changing action."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models.entities import AuditEvent
from ...storage.metadata.interface import MetadataStore


class AuditService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def record(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        operation_id: str | None,
        now: datetime,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=self.store.allocate_id("ae"),
            operation_id=operation_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            actor=actor,
            created_at=now,
        )
        self.store.insert_audit_event(event)
        return event

    def for_entity(self, entity_id: str) -> list[dict[str, Any]]:
        return [self._to_dict(e) for e in self.store.list_audit_events(entity_id=entity_id)]

    def for_operation(self, operation_id: str) -> list[dict[str, Any]]:
        return [self._to_dict(e) for e in self.store.list_audit_events(operation_id=operation_id)]

    @staticmethod
    def _to_dict(event: AuditEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "event": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "operation_id": event.operation_id,
            "actor": event.actor,
            "details": event.details,
            "at": event.created_at.isoformat(),
        }
