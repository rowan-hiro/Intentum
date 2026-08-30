"""Validate a canonical ``TransformIR`` independently of how it was produced.

The validator re-derives every type and every step schema using the shared
type rules and rejects any IR whose declared types/schemas disagree. This is
the deterministic gate before planning: nothing that fails here is executed.
"""

from __future__ import annotations

from ..errors import InvalidSchemaError, InvalidStateError, InvalidTransformError, NotFoundError, TypeMismatchError
from ..ir import (
    AggregateStep,
    BinaryExpr,
    CastExpr,
    ColumnExpr,
    DeriveStep,
    Expr,
    FieldRef,
    FilterStep,
    FunctionExpr,
    InExpr,
    JoinStep,
    LimitStep,
    LiteralExpr,
    OutputMode,
    RenameStep,
    SelectStep,
    SortStep,
    TransformIR,
    UnaryExpr,
)
from ..ir.typing import (
    aggregate_result_type,
    binary_result_type,
    comparable,
    function_result_type,
    literal_type,
    unary_result_type,
    validate_function_arguments,
)
from ..models.entities import Dataset, DatasetStatus, LogicalType
from ..naming import is_identifier
from ...storage.metadata.interface import MetadataStore


class _NameRule:
    """Adapter so identifier checks read like a regex ``fullmatch``."""

    @staticmethod
    def fullmatch(name: str) -> bool:
        return is_identifier(name)


NAME_RE = _NameRule()


class IRValidator:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def validate_transform(self, ir: TransformIR) -> None:
        source = self._dataset(ir.source.dataset_id, ir.source.version, "source")
        expected_input = [FieldRef(name=c.name, logical_type=c.logical_type, column_id=c.id) for c in source.columns]
        if list(ir.input_schema) != expected_input:
            raise InvalidSchemaError(
                f"Input schema in IR does not match the current schema of {source.name} ({source.id}).",
                field="source",
                hint="The dataset changed since the intent was resolved; resubmit the intent.",
            )
        scope = list(ir.input_schema)
        for index, step in enumerate(ir.steps):
            scope = self._validate_step(step, scope, index)
            if list(step.output_schema) != scope:
                raise InvalidTransformError(
                    f"Declared output schema of step {index} ({step.type}) does not match the computed schema.",
                    field=f"steps[{index}]",
                )
        if list(ir.output_schema) != scope:
            raise InvalidTransformError("Declared IR output schema does not match the computed schema.", field="output_schema")
        if not scope:
            raise InvalidTransformError("The transform produces no columns.", field="steps")
        if ir.output.mode == OutputMode.MATERIALIZED:
            if not ir.output.name or not NAME_RE.fullmatch(ir.output.name):
                raise InvalidTransformError("Materialized outputs need a snake_case name.", field="output.name")

    # -- steps -----------------------------------------------------------
    def _validate_step(self, step, scope: list[FieldRef], index: int) -> list[FieldRef]:
        where = f"steps[{index}]"
        by_name = {f.name: f for f in scope}

        def require(ref: FieldRef, what: str) -> FieldRef:
            current = by_name.get(ref.name)
            if current is None:
                raise NotFoundError(f"{what} references unknown field {ref.name!r}.", field=where,
                                    candidates=[f.name for f in scope])
            if current != ref:
                raise TypeMismatchError(
                    f"{what} field {ref.name!r} is declared as {ref.logical_type} but is {current.logical_type}.", field=where
                )
            return current

        if isinstance(step, SelectStep):
            fields = [require(f, "select") for f in step.fields]
            self._unique([f.name for f in fields], where)
            return fields
        if isinstance(step, FilterStep):
            t = self._expr_type(step.predicate, by_name, where)
            if t != LogicalType.BOOLEAN:
                raise TypeMismatchError(f"filter predicate must be boolean, got {t}.", field=where)
            return scope
        if isinstance(step, AggregateStep):
            group = [require(f, "group_by") for f in step.group_by]
            out = list(group)
            for m in step.measures:
                operand = require(m.field, f"measure {m.alias}").logical_type if m.field else None
                expected = aggregate_result_type(m.function, operand)
                if expected != m.logical_type:
                    raise TypeMismatchError(f"measure {m.alias!r} should be {expected}, declared {m.logical_type}.", field=where)
                if not NAME_RE.fullmatch(m.alias):
                    raise InvalidTransformError(f"measure alias {m.alias!r} must be snake_case.", field=where)
                out.append(FieldRef(name=m.alias, logical_type=m.logical_type))
            self._unique([f.name for f in out], where)
            return out
        if isinstance(step, SortStep):
            for key in step.keys:
                require(key.field, "sort")
            return scope
        if isinstance(step, LimitStep):
            return scope
        if isinstance(step, RenameStep):
            renamed = {}
            for m in step.mappings:
                require(m.field, "rename")
                if not NAME_RE.fullmatch(m.to):
                    raise InvalidTransformError(f"rename target {m.to!r} must be snake_case.", field=where)
                renamed[m.field.name] = m.to
            out = [FieldRef(name=renamed.get(f.name, f.name), logical_type=f.logical_type, column_id=f.column_id) for f in scope]
            self._unique([f.name for f in out], where)
            return out
        if isinstance(step, DeriveStep):
            t = self._expr_type(step.expression, by_name, where)
            if t != step.logical_type:
                raise TypeMismatchError(f"derived field {step.name!r} should be {t}, declared {step.logical_type}.", field=where)
            if not NAME_RE.fullmatch(step.name):
                raise InvalidTransformError(f"derived field name {step.name!r} must be snake_case.", field=where)
            out = scope + [FieldRef(name=step.name, logical_type=t)]
            self._unique([f.name for f in out], where)
            return out
        if isinstance(step, JoinStep):
            right = self._dataset(step.right.dataset_id, step.right.version, f"{where}.right")
            right_fields = {c.name: FieldRef(name=c.name, logical_type=c.logical_type, column_id=c.id) for c in right.columns}
            for cond in step.on:
                left = require(cond.left, "join")
                actual_right = right_fields.get(cond.right.name)
                if actual_right is None or actual_right != cond.right:
                    raise NotFoundError(f"join references unknown right field {cond.right.name!r}.", field=where,
                                        candidates=list(right_fields))
                if not comparable(left.logical_type, actual_right.logical_type):
                    raise TypeMismatchError(f"join keys {left.name} and {cond.right.name} are not comparable.", field=where)
            out = list(scope)
            for jo in step.right_fields:
                actual = right_fields.get(jo.field.name)
                if actual is None or actual != jo.field:
                    raise NotFoundError(f"join output references unknown right field {jo.field.name!r}.", field=where)
                out.append(FieldRef(name=jo.output_name, logical_type=actual.logical_type, column_id=actual.column_id))
            self._unique([f.name for f in out], where)
            return out
        raise InvalidTransformError(f"Unknown step type {type(step).__name__}.", field=where)

    # -- expressions -----------------------------------------------------
    def _expr_type(self, expr: Expr, by_name: dict[str, FieldRef], where: str) -> LogicalType:
        if isinstance(expr, ColumnExpr):
            current = by_name.get(expr.field.name)
            if current is None:
                raise NotFoundError(f"expression references unknown field {expr.field.name!r}.", field=where,
                                    candidates=list(by_name))
            if current != expr.field or expr.logical_type != current.logical_type:
                raise TypeMismatchError(f"field {expr.field.name!r} has type {current.logical_type}, IR says {expr.logical_type}.", field=where)
            return current.logical_type
        if isinstance(expr, LiteralExpr):
            if literal_type(expr.value) != expr.logical_type:
                raise TypeMismatchError(f"literal {expr.value!r} is not {expr.logical_type}.", field=where)
            return expr.logical_type
        if isinstance(expr, BinaryExpr):
            t = binary_result_type(expr.op, self._expr_type(expr.left, by_name, where), self._expr_type(expr.right, by_name, where))
            self._declared(t, expr.logical_type, where)
            return t
        if isinstance(expr, UnaryExpr):
            t = unary_result_type(expr.op, self._expr_type(expr.operand, by_name, where))
            self._declared(t, expr.logical_type, where)
            return t
        if isinstance(expr, FunctionExpr):
            validate_function_arguments(expr.name, expr.args)
            t = function_result_type(expr.name, [self._expr_type(a, by_name, where) for a in expr.args])
            self._declared(t, expr.logical_type, where)
            return t
        if isinstance(expr, InExpr):
            t = self._expr_type(expr.expr, by_name, where)
            for v in expr.values:
                if not comparable(t, v.logical_type):
                    raise TypeMismatchError(f"IN list value {v.value!r} is not comparable with {t}.", field=where)
            return LogicalType.BOOLEAN
        if isinstance(expr, CastExpr):
            self._expr_type(expr.expr, by_name, where)
            return expr.logical_type
        raise InvalidTransformError(f"Unknown expression node {type(expr).__name__}.", field=where)

    @staticmethod
    def _declared(actual: LogicalType, declared: LogicalType, where: str) -> None:
        if actual != declared:
            raise TypeMismatchError(f"expression type {actual} does not match declared {declared}.", field=where)

    @staticmethod
    def _unique(names: list[str], where: str) -> None:
        if len(set(names)) != len(names):
            raise InvalidTransformError("Duplicate output field names.", field=where)

    def _dataset(self, dataset_id: str, version: int, field: str) -> Dataset:
        dataset = self.store.get_dataset(dataset_id, include_deleted=True)
        if dataset is None:
            raise NotFoundError(f"Dataset {dataset_id} does not exist.", field=field)
        if dataset.status == DatasetStatus.DELETED:
            raise NotFoundError(
                f"Dataset {dataset.name} ({dataset.id}) is deleted.", field=field,
                details={"dataset_id": dataset.id, "restorable": True},
                hint=f"Call restore_dataset with dataset={dataset.id!r} first.",
            )
        if dataset.version != version:
            raise InvalidStateError(
                f"Dataset {dataset.name} is now at version {dataset.version}, the intent referenced version {version}.",
                field=field,
                hint="Resubmit the intent so it is resolved against the current version.",
            )
        return dataset
