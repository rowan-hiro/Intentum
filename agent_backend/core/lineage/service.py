"""Lineage recording and traversal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models.entities import LineageEdge, Relationship
from ...storage.metadata.interface import MetadataStore


class LineageService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def record(
        self,
        *,
        source_dataset_id: str,
        source_version: int,
        target_dataset_id: str,
        target_version: int,
        operation_id: str,
        relationship: Relationship,
        now: datetime,
    ) -> LineageEdge:
        edge = LineageEdge(
            id=self.store.allocate_id("le"),
            source_dataset_id=source_dataset_id,
            source_version=source_version,
            target_dataset_id=target_dataset_id,
            target_version=target_version,
            operation_id=operation_id,
            relationship=relationship,
            created_at=now,
        )
        self.store.insert_lineage_edge(edge)
        return edge

    def upstream(self, dataset_id: str, depth: int = 10) -> list[dict[str, Any]]:
        """Return upstream edges transitively (nearest first)."""
        seen: set[str] = set()
        frontier = [dataset_id]
        result: list[dict[str, Any]] = []
        for _ in range(depth):
            next_frontier: list[str] = []
            for current in frontier:
                for edge in self.store.lineage_into(current):
                    key = edge.id
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append(self._edge_dict(edge))
                    next_frontier.append(edge.source_dataset_id)
            if not next_frontier:
                break
            frontier = next_frontier
        return result

    def downstream(self, dataset_id: str, depth: int = 10) -> list[dict[str, Any]]:
        seen: set[str] = set()
        frontier = [dataset_id]
        result: list[dict[str, Any]] = []
        for _ in range(depth):
            next_frontier: list[str] = []
            for current in frontier:
                for edge in self.store.lineage_out_of(current):
                    if edge.id in seen:
                        continue
                    seen.add(edge.id)
                    result.append(self._edge_dict(edge))
                    next_frontier.append(edge.target_dataset_id)
            if not next_frontier:
                break
            frontier = next_frontier
        return result

    def _edge_dict(self, edge: LineageEdge) -> dict[str, Any]:
        source = self.store.get_dataset(edge.source_dataset_id, include_deleted=True)
        target = self.store.get_dataset(edge.target_dataset_id, include_deleted=True)
        return {
            "source": edge.source_dataset_id,
            "source_name": source.name if source else None,
            "source_version": edge.source_version,
            "target": edge.target_dataset_id,
            "target_name": target.name if target else None,
            "target_version": edge.target_version,
            "operation_id": edge.operation_id,
            "relationship": str(edge.relationship),
            "created_at": edge.created_at.isoformat(),
        }
