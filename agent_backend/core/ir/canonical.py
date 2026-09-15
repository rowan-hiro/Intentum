"""Canonical intermediate representation.

This is the strict, typed contract between the fuzzy agent world and the
deterministic execution world. Every model forbids unknown fields; every
column reference is resolved to a concrete field of a concrete relation; every
expression carries its inferred logical type. The planner and executor only
ever see this representation, never the loose agent intent.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.entities import LogicalType


class IRModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_ ]*$"


class DatasetRef(IRModel):
    dataset_id: str
    version: int = Field(ge=1)
    name: str


class FieldRef(IRModel):
    """A field of the relation that is current at a given pipeline position.

    ``column_id`` is set when the field traces directly back to a physical
    dataset column; derived/aggregated fields have no column id.
    """

    name: str = Field(min_length=1)
    logical_type: LogicalType
    column_id: str | None = None


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------

ArithmeticOp = Literal["+", "-", "*", "/", "%"]
ComparisonOp = Literal["=", "!=", "<", "<=", ">", ">="]
BooleanOp = Literal["and", "or"]
BinaryOp = Literal["+", "-", "*", "/", "%", "=", "!=", "<", "<=", ">", ">=", "and", "or"]


class ColumnExpr(IRModel):
    kind: Literal["column"] = "column"
    field: FieldRef
    logical_type: LogicalType


class LiteralExpr(IRModel):
    kind: Literal["literal"] = "literal"
    value: int | float | str | bool | None
    logical_type: LogicalType


class BinaryExpr(IRModel):
    kind: Literal["binary"] = "binary"
    op: BinaryOp
    left: Expr
    right: Expr
    logical_type: LogicalType


class UnaryExpr(IRModel):
    kind: Literal["unary"] = "unary"
    op: Literal["not", "neg"]
    operand: Expr
    logical_type: LogicalType


class FunctionExpr(IRModel):
    kind: Literal["function"] = "function"
    name: str
    args: list[Expr] = Field(default_factory=list)
    logical_type: LogicalType


class InExpr(IRModel):
    kind: Literal["in"] = "in"
    expr: Expr
    values: list[LiteralExpr] = Field(min_length=1)
    negated: bool = False
    logical_type: LogicalType = LogicalType.BOOLEAN


class CastExpr(IRModel):
    kind: Literal["cast"] = "cast"
    expr: Expr
    logical_type: LogicalType


class TryCastExpr(IRModel):
    """A cast that yields null for a value that does not convert, where a cast fails the transform."""

    kind: Literal["try_cast"] = "try_cast"
    expr: Expr
    logical_type: LogicalType


Expr = Annotated[
    Union[ColumnExpr, LiteralExpr, BinaryExpr, UnaryExpr, FunctionExpr, InExpr, CastExpr, TryCastExpr],
    Field(discriminator="kind"),
]

for _model in (BinaryExpr, UnaryExpr, FunctionExpr, InExpr, CastExpr, TryCastExpr):
    _model.model_rebuild()


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class AggregateFunction(StrEnum):
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"


class _StepBase(IRModel):
    output_schema: list[FieldRef]


class SelectStep(_StepBase):
    type: Literal["select"] = "select"
    fields: list[FieldRef] = Field(min_length=1)


class FilterStep(_StepBase):
    type: Literal["filter"] = "filter"
    predicate: Expr


class Measure(IRModel):
    function: AggregateFunction
    field: FieldRef | None = None
    alias: str = Field(min_length=1)
    logical_type: LogicalType


class AggregateStep(_StepBase):
    type: Literal["aggregate"] = "aggregate"
    group_by: list[FieldRef] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)


class SortKey(IRModel):
    field: FieldRef
    direction: Literal["asc", "desc"] = "asc"


class SortStep(_StepBase):
    type: Literal["sort"] = "sort"
    keys: list[SortKey] = Field(min_length=1)


class LimitStep(_StepBase):
    type: Literal["limit"] = "limit"
    limit: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)


class RenameMapping(IRModel):
    field: FieldRef
    to: str = Field(min_length=1)


class RenameStep(_StepBase):
    type: Literal["rename"] = "rename"
    mappings: list[RenameMapping] = Field(min_length=1)


class DeriveStep(_StepBase):
    type: Literal["derive"] = "derive"
    name: str = Field(min_length=1)
    expression: Expr
    logical_type: LogicalType


class JoinCondition(IRModel):
    left: FieldRef
    right: FieldRef


class JoinOutput(IRModel):
    field: FieldRef
    output_name: str


class JoinStep(_StepBase):
    type: Literal["join"] = "join"
    right: DatasetRef
    how: Literal["inner", "left", "right", "full"] = "inner"
    on: list[JoinCondition] = Field(min_length=1)
    right_fields: list[JoinOutput]


class SemiJoinStep(_StepBase):
    type: Literal["semi_join"] = "semi_join"
    right: DatasetRef
    on: list[JoinCondition] = Field(min_length=1)


class QueryInput(IRModel):
    """A placeholder of a raw_query and the dataset version bound to it, with the fields it exposes."""

    placeholder: str = Field(min_length=1)
    dataset: DatasetRef
    fields: list[FieldRef] = Field(min_length=1)


class RawQueryStep(_StepBase):
    """Read-only SQL over placeholder-bound inputs: the fallback for shapes the steps cannot express (MADR 0002).

    Always the first step; ``inputs[0]`` binds ``input`` to the transform's
    source. ``columns`` are the names the statement returns, in order, and
    ``output_schema`` the fields they become.
    """

    type: Literal["raw_query"] = "raw_query"
    sql: str = Field(min_length=1)
    inputs: list[QueryInput] = Field(min_length=1)
    columns: list[str] = Field(min_length=1)


Step = Annotated[
    Union[SelectStep, FilterStep, AggregateStep, SortStep, LimitStep, RenameStep, DeriveStep, JoinStep, SemiJoinStep,
          RawQueryStep],
    Field(discriminator="type"),
]


class OutputMode(StrEnum):
    PREVIEW = "preview"
    MATERIALIZED = "materialized"


class OutputSpec(IRModel):
    mode: OutputMode
    name: str | None = None
    description: str = ""
    preview_limit: int = Field(default=20, ge=0, le=1000)


class TransformIR(IRModel):
    operation: Literal["transform"] = "transform"
    source: DatasetRef
    steps: list[Step] = Field(default_factory=list)
    input_schema: list[FieldRef]
    output_schema: list[FieldRef]
    output: OutputSpec

    def logical_fingerprint(self) -> str:
        """Stable hash of the logical request, used for idempotency.

        The preview limit is presentation-only and excluded.
        """
        payload = self.model_dump(mode="json")
        payload["output"].pop("preview_limit", None)
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def referenced_datasets(self) -> list[DatasetRef]:
        refs = [self.source]
        for step in self.steps:
            if isinstance(step, (JoinStep, SemiJoinStep)):
                refs.append(step.right)
            elif isinstance(step, RawQueryStep):
                # Every bound input is an upstream; the source is bound as ``input`` and listed once.
                for binding in step.inputs:
                    if binding.dataset not in refs:
                        refs.append(binding.dataset)
        return refs

    @property
    def uses_raw_query(self) -> bool:
        return any(isinstance(step, RawQueryStep) for step in self.steps)


class ImportIR(IRModel):
    operation: Literal["import"] = "import"
    source_path: str
    format: Literal["csv", "parquet", "json", "sqlite"]
    locator: str | None = None  # table inside a multi-table container such as SQLite
    name: str
    description: str = ""
    content_hash: str
    column_hints: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Types the import itself refined (text → date/timestamp); replayable, and
    # excluded from the fingerprint because they are derived from the source.
    refined_types: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        from ..naming import is_identifier

        if not is_identifier(value):
            raise ValueError("dataset names must be normalized identifiers (casefolded snake_case, any script)")
        return value

    def logical_fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("refined_types", None)
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
