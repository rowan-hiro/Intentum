"""Resolve loose field names against the schema that is current at a step."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field as dc_field
from typing import Any

from ..errors import AmbiguousReferenceError, InvalidIntentError, NotFoundError
from ..ir.canonical import FieldRef
from ..models.entities import Column, LogicalType, SemanticRole
from .common import ResolutionNote, normalize

# Small built-in synonym groups. A term only resolves through a group when it
# maps to exactly one field; otherwise the ambiguity is surfaced.
SYNONYM_GROUPS: list[set[str]] = [
    {"revenue", "sales", "amount", "order_amount", "total", "total_amount", "sales_amount", "turnover"},
    {"quantity", "qty", "units", "unit_count"},
    {"price", "unit_price"},
    {"customer", "customer_name", "client", "buyer"},
    {"date", "order_date", "day", "timestamp", "time", "created_at"},
    {"region", "area", "territory", "zone"},
    {"product", "item", "sku", "product_name"},
]


@dataclass
class ScopeField:
    name: str
    logical_type: LogicalType
    column_id: str | None = None
    aliases: list[str] = dc_field(default_factory=list)
    semantic_role: SemanticRole = SemanticRole.UNKNOWN

    def ref(self) -> FieldRef:
        return FieldRef(name=self.name, logical_type=self.logical_type, column_id=self.column_id)

    @classmethod
    def from_column(cls, column: Column) -> "ScopeField":
        return cls(
            name=column.name,
            logical_type=column.logical_type,
            column_id=column.id,
            aliases=list(column.aliases),
            semantic_role=column.semantic_role,
        )

    @classmethod
    def from_ref(cls, ref: FieldRef) -> "ScopeField":
        return cls(name=ref.name, logical_type=ref.logical_type, column_id=ref.column_id)


@dataclass
class Scope:
    fields: list[ScopeField]

    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    def refs(self) -> list[FieldRef]:
        return [f.ref() for f in self.fields]

    def get(self, name: str) -> ScopeField | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def with_fields(self, fields: list[ScopeField]) -> "Scope":
        return Scope(fields=fields)


class FieldResolver:
    def resolve(
        self,
        reference: Any,
        scope: Scope,
        *,
        field: str,
        notes: list[ResolutionNote] | None = None,
    ) -> ScopeField:
        text = self._reference_text(reference, field)
        # 1. exact
        exact = scope.get(text)
        if exact is not None:
            return exact
        lowered = text.lower()
        # 2. case-insensitive / alias / normalized
        for f in scope.fields:
            if f.name.lower() == lowered or lowered in (a.lower() for a in f.aliases):
                self._note(notes, field, text, f, "case-insensitive/alias match")
                return f
        norm = normalize(text)
        normalized = [f for f in scope.fields if normalize(f.name) == norm or norm in (normalize(a) for a in f.aliases)]
        if len(normalized) == 1:
            self._note(notes, field, text, normalized[0], "normalized name match")
            return normalized[0]
        # 3. synonym groups
        for group in SYNONYM_GROUPS:
            if norm in group:
                hits = [f for f in scope.fields if normalize(f.name) in group]
                if len(hits) == 1:
                    self._note(notes, field, text, hits[0], f"synonym of {hits[0].name!r}")
                    return hits[0]
                if len(hits) > 1:
                    raise AmbiguousReferenceError(
                        f"The field {text!r} could mean any of {[h.name for h in hits]}.",
                        field=field,
                        candidates=[self._candidate(h) for h in hits],
                    )
        # 4. fuzzy
        close = difflib.get_close_matches(norm, [normalize(f.name) for f in scope.fields], n=3, cutoff=0.8)
        hits = [f for f in scope.fields if normalize(f.name) in close]
        if len(hits) == 1:
            self._note(notes, field, text, hits[0], "fuzzy name match")
            return hits[0]
        if len(hits) > 1:
            raise AmbiguousReferenceError(
                f"The field {text!r} is ambiguous between {[h.name for h in hits]}.",
                field=field,
                candidates=[self._candidate(h) for h in hits],
            )
        raise NotFoundError(
            f"No field named {text!r} is available at this step.",
            field=field,
            candidates=[self._candidate(f) for f in scope.fields],
            hint="Use one of the listed field names.",
        )

    def resolve_measure_field(
        self,
        reference: Any,
        scope: Scope,
        *,
        field: str,
        notes: list[ResolutionNote] | None = None,
    ) -> ScopeField:
        """Like ``resolve`` but, when the name is unknown, falls back to the
        single numeric measure column if there is exactly one."""
        try:
            return self.resolve(reference, scope, field=field, notes=notes)
        except NotFoundError as err:
            measures = [
                f for f in scope.fields
                if f.logical_type.is_numeric and f.semantic_role in (SemanticRole.MEASURE, SemanticRole.UNKNOWN)
            ]
            if len(measures) == 1:
                self._note(notes, field, str(reference), measures[0], "only numeric measure in scope")
                return measures[0]
            if len(measures) > 1:
                raise AmbiguousReferenceError(
                    f"The metric {reference!r} does not name a field; candidates are {[m.name for m in measures]}.",
                    field=field,
                    candidates=[self._candidate(m) for m in measures],
                ) from err
            raise

    @staticmethod
    def _reference_text(reference: Any, field: str) -> str:
        if isinstance(reference, str) and reference.strip():
            return reference.strip()
        if isinstance(reference, dict):
            for key in ("column", "field", "name", "column_id", "id"):
                value = reference.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        raise InvalidIntentError(f"{field} must be a field name.", field=field)

    @staticmethod
    def _note(notes: list[ResolutionNote] | None, field: str, text: str, resolved: ScopeField, reason: str) -> None:
        if notes is not None:
            notes.append(ResolutionNote(field, text, resolved.name, reason))

    @staticmethod
    def _candidate(f: ScopeField) -> dict[str, Any]:
        item: dict[str, Any] = {"name": f.name, "type": str(f.logical_type)}
        if f.column_id:
            item["id"] = f.column_id
        if f.semantic_role != SemanticRole.UNKNOWN:
            item["role"] = str(f.semantic_role)
        return item
