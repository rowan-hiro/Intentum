"""Unit rules: which unit a field of a transform carries, and where two declared units meet.

A column's unit is what was declared for it (an import hint, ``update_metadata`` or ``attach_metadata``); the
backend never infers one. Units are compared by their written form, case-folded with whitespace collapsed, so
``USD`` and ``usd`` agree and ``USD`` and ``EUR``, or ``元`` and ``万元``, do not: the backend converts nothing,
not currencies and not scale factors. An empty unit is unknown, never dimensionless.

The rules follow a canonical transform step by step:

* a field keeps its unit through select, filter, sort, limit, rename and semi_join; a join brings each right
  column's own unit;
* ``sum``, ``avg``, ``min`` and ``max`` keep their field's unit and ``count`` gives none;
* in an expression, a column has its unit and a literal has none; ``+`` and ``-`` give the unit both operands
  agree on and nothing when either is unknown; ``abs``, ``round``, ``floor``, ``ceil``, negation and a cast to a
  number keep their operand's unit; every other operation gives none;
* a raw_query's output columns have no unit.

Adding, subtracting or comparing two operands whose units are both declared and differ is a ``UnitConflict``,
and the result has no unit. Conflicts are reported, never refused (MADR 0019).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..models.entities import LogicalType
from .canonical import (
    AggregateFunction,
    AggregateStep,
    BinaryExpr,
    CastExpr,
    ColumnExpr,
    DeriveStep,
    Expr,
    FilterStep,
    FunctionExpr,
    InExpr,
    JoinStep,
    RawQueryStep,
    RenameStep,
    TransformIR,
    TryCastExpr,
    UnaryExpr,
)

CHECKED_OPS = ("+", "-", "=", "!=", "<", "<=", ">", ">=")
_KEEPING_FUNCTIONS = ("abs", "round", "floor", "ceil")
_KEEPING_MEASURES = (AggregateFunction.SUM, AggregateFunction.AVG, AggregateFunction.MIN, AggregateFunction.MAX)


def same_unit(a: str, b: str) -> bool:
    """Whether two declared units are one unit as written: case-folded, whitespace collapsed."""
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


@dataclass(frozen=True)
class UnitConflict:
    """Two operands with different declared units met in an addition, subtraction or comparison."""

    position: int  # the step's index among the transform's steps
    step: str  # the step type: derive or filter
    name: str | None  # the derived field, for a derive
    op: str
    left: Expr
    left_unit: str
    right: Expr
    right_unit: str


@dataclass
class UnitReport:
    """The unit of each output field ("" when unknown) and the conflicts met on the way."""

    units: dict[str, str] = field(default_factory=dict)
    conflicts: list[UnitConflict] = field(default_factory=list)


def infer_units(ir: TransformIR, column_unit: Callable[[str | None], str]) -> UnitReport:
    """Follow the declared units through the transform. ``column_unit`` gives a source column's declared unit."""
    report = UnitReport()
    units = {f.name: column_unit(f.column_id) for f in ir.input_schema}
    for position, step in enumerate(ir.steps):
        if isinstance(step, RawQueryStep):
            current: dict[str, str] = {}
        elif isinstance(step, FilterStep):
            _unit(step.predicate, units, report, position, "filter", None)
            current = units
        elif isinstance(step, DeriveStep):
            current = {**units, step.name: _unit(step.expression, units, report, position, "derive", step.name)}
        elif isinstance(step, AggregateStep):
            current = {f.name: units.get(f.name, "") for f in step.group_by}
            for m in step.measures:
                keeps = m.field is not None and m.function in _KEEPING_MEASURES
                current[m.alias] = units.get(m.field.name, "") if keeps and m.field else ""
        elif isinstance(step, RenameStep):
            renamed = {m.field.name: m.to for m in step.mappings}
            current = {renamed.get(name, name): unit for name, unit in units.items()}
        elif isinstance(step, JoinStep):
            current = {**units, **{jo.output_name: column_unit(jo.field.column_id) for jo in step.right_fields}}
        else:  # select, sort, limit, semi_join keep what they keep
            current = units
        units = {f.name: current.get(f.name, "") for f in step.output_schema}
    report.units = {f.name: units.get(f.name, "") for f in ir.output_schema}
    return report


def _unit(expr: Expr, units: dict[str, str], report: UnitReport, position: int, step: str, name: str | None) -> str:
    """The unit an expression carries, recording every conflict inside it."""

    def walk(e: Expr) -> str:
        if isinstance(e, ColumnExpr):
            return units.get(e.field.name, "")
        if isinstance(e, BinaryExpr):
            left, right = walk(e.left), walk(e.right)
            if e.op not in CHECKED_OPS:
                return ""
            if left and right and not same_unit(left, right):
                conflict = UnitConflict(position, step, name, e.op, e.left, left, e.right, right)
                if conflict not in report.conflicts:
                    report.conflicts.append(conflict)
                return ""
            return left if e.op in ("+", "-") and left and right else ""
        if isinstance(e, UnaryExpr):
            operand = walk(e.operand)
            return operand if e.op == "neg" else ""
        if isinstance(e, FunctionExpr):
            args = [walk(a) for a in e.args]
            return args[0] if e.name in _KEEPING_FUNCTIONS and args else ""
        if isinstance(e, (CastExpr, TryCastExpr)):
            operand = walk(e.expr)
            return operand if e.logical_type in (LogicalType.INTEGER, LogicalType.FLOAT) else ""
        if isinstance(e, InExpr):
            walk(e.expr)
        return ""

    return walk(expr)
