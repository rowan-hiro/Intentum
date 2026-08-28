"""Type rules shared by the resolver (inference) and the validator (re-check).

Keeping the rules in one place guarantees that the validator recomputes
exactly what the resolver inferred, so a tampered or hand-written canonical IR
that disagrees with these rules is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..errors import InvalidTransformError, TypeMismatchError
from ..models.entities import LogicalType
from .canonical import AggregateFunction

NUMERIC = frozenset({LogicalType.INTEGER, LogicalType.FLOAT})
TEMPORAL = frozenset({LogicalType.DATE, LogicalType.TIMESTAMP})
STRINGY = frozenset({LogicalType.STRING})
COMPARISON_OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})
ARITHMETIC_OPS = frozenset({"+", "-", "*", "/", "%"})
BOOLEAN_OPS = frozenset({"and", "or"})


@dataclass(frozen=True)
class ArgSpec:
    allowed: frozenset[LogicalType]
    optional: bool = False


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    args: tuple[ArgSpec, ...]
    returns: Callable[[list[LogicalType]], LogicalType]
    variadic: bool = False
    sql: str | None = None  # SQL template, defaults to name(args)


ANY = frozenset(set(LogicalType))
_num = ArgSpec(NUMERIC)
_str = ArgSpec(STRINGY)
_tmp = ArgSpec(TEMPORAL)
_any = ArgSpec(ANY)


def _same(types: list[LogicalType]) -> LogicalType:
    return types[0]


def _float(_: list[LogicalType]) -> LogicalType:
    return LogicalType.FLOAT


def _int(_: list[LogicalType]) -> LogicalType:
    return LogicalType.INTEGER


def _string(_: list[LogicalType]) -> LogicalType:
    return LogicalType.STRING


def _bool(_: list[LogicalType]) -> LogicalType:
    return LogicalType.BOOLEAN


FUNCTIONS: dict[str, FunctionSpec] = {
    spec.name: spec
    for spec in (
        FunctionSpec("abs", (_num,), _same),
        FunctionSpec("round", (_num, ArgSpec(frozenset({LogicalType.INTEGER}), optional=True)), _float),
        FunctionSpec("floor", (_num,), _float),
        FunctionSpec("ceil", (_num,), _float),
        FunctionSpec("upper", (_str,), _string),
        FunctionSpec("lower", (_str,), _string),
        FunctionSpec("trim", (_str,), _string),
        FunctionSpec("length", (_str,), _int),
        FunctionSpec("concat", (_any, _any), _string, variadic=True),
        FunctionSpec("coalesce", (_any, _any), _same, variadic=True),
        FunctionSpec("year", (_tmp,), _int),
        FunctionSpec("month", (_tmp,), _int),
        FunctionSpec("day", (_tmp,), _int),
        FunctionSpec("is_null", (_any,), _bool, sql="({0} IS NULL)"),
        FunctionSpec("contains", (_str, _str), _bool),
        FunctionSpec("starts_with", (_str, _str), _bool),
        FunctionSpec("ends_with", (_str, _str), _bool),
    )
}


def describe_type(t: LogicalType) -> str:
    return str(t)


def function_result_type(name: str, arg_types: list[LogicalType]) -> LogicalType:
    spec = FUNCTIONS.get(name)
    if spec is None:
        raise InvalidTransformError(
            f"Unknown function {name!r}.",
            field="expression",
            details={"allowed_functions": sorted(FUNCTIONS)},
        )
    required = [a for a in spec.args if not a.optional]
    if len(arg_types) < len(required) or (not spec.variadic and len(arg_types) > len(spec.args)):
        raise InvalidTransformError(
            f"Function {name!r} expects {len(required)}"
            + ("" if len(required) == len(spec.args) and not spec.variadic else " or more")
            + f" argument(s), got {len(arg_types)}.",
            field="expression",
        )
    for index, actual in enumerate(arg_types):
        spec_arg = spec.args[min(index, len(spec.args) - 1)]
        if actual not in spec_arg.allowed and actual != LogicalType.UNKNOWN:
            raise TypeMismatchError(
                f"Argument {index + 1} of {name!r} has type {actual}, expected one of "
                f"{sorted(str(t) for t in spec_arg.allowed)}.",
                field="expression",
            )
    if spec.name == "coalesce":
        non_unknown = [t for t in arg_types if t != LogicalType.UNKNOWN]
        base = non_unknown[0] if non_unknown else LogicalType.UNKNOWN
        for t in non_unknown:
            if t != base and not (t in NUMERIC and base in NUMERIC):
                raise TypeMismatchError(f"coalesce arguments must share a type; got {base} and {t}.", field="expression")
        return LogicalType.FLOAT if base in NUMERIC and LogicalType.FLOAT in non_unknown else base
    return spec.returns(arg_types)


def binary_result_type(op: str, left: LogicalType, right: LogicalType) -> LogicalType:
    if op in ARITHMETIC_OPS:
        if left in NUMERIC and right in NUMERIC:
            if op == "/":
                return LogicalType.FLOAT
            if left == LogicalType.INTEGER and right == LogicalType.INTEGER:
                return LogicalType.INTEGER
            return LogicalType.FLOAT
        hint = "Use concat(a, b) to combine strings." if op == "+" and LogicalType.STRING in (left, right) else None
        raise TypeMismatchError(
            f"Operator {op!r} requires numeric operands, got {left} and {right}.",
            field="expression",
            hint=hint,
        )
    if op in COMPARISON_OPS:
        if comparable(left, right):
            return LogicalType.BOOLEAN
        raise TypeMismatchError(
            f"Cannot compare {left} with {right} using {op!r}.",
            field="expression",
        )
    if op in BOOLEAN_OPS:
        if left == LogicalType.BOOLEAN and right == LogicalType.BOOLEAN:
            return LogicalType.BOOLEAN
        raise TypeMismatchError(
            f"Operator {op!r} requires boolean operands, got {left} and {right}.",
            field="expression",
        )
    raise InvalidTransformError(f"Unknown operator {op!r}.", field="expression")


def comparable(left: LogicalType, right: LogicalType) -> bool:
    if LogicalType.UNKNOWN in (left, right):
        return True
    if left == right:
        return True
    if left in NUMERIC and right in NUMERIC:
        return True
    if left in TEMPORAL and right in TEMPORAL:
        return True
    return False


def unary_result_type(op: str, operand: LogicalType) -> LogicalType:
    if op == "not":
        if operand != LogicalType.BOOLEAN:
            raise TypeMismatchError(f"'not' requires a boolean operand, got {operand}.", field="expression")
        return LogicalType.BOOLEAN
    if op == "neg":
        if operand not in NUMERIC:
            raise TypeMismatchError(f"Negation requires a numeric operand, got {operand}.", field="expression")
        return operand
    raise InvalidTransformError(f"Unknown unary operator {op!r}.", field="expression")


def aggregate_result_type(function: AggregateFunction, operand: LogicalType | None) -> LogicalType:
    if function in (AggregateFunction.COUNT, AggregateFunction.COUNT_DISTINCT):
        return LogicalType.INTEGER
    if operand is None:
        raise InvalidTransformError(f"Aggregate {function} requires a field.", field="measures")
    if function == AggregateFunction.AVG:
        if operand not in NUMERIC:
            raise TypeMismatchError(f"avg requires a numeric field, got {operand}.", field="measures")
        return LogicalType.FLOAT
    if function == AggregateFunction.SUM:
        if operand not in NUMERIC:
            raise TypeMismatchError(f"sum requires a numeric field, got {operand}.", field="measures")
        return operand
    if function in (AggregateFunction.MIN, AggregateFunction.MAX):
        if operand == LogicalType.BOOLEAN:
            raise TypeMismatchError(f"{function} is not defined for boolean fields.", field="measures")
        return operand
    raise InvalidTransformError(f"Unknown aggregate function {function!r}.", field="measures")


def literal_type(value: object) -> LogicalType:
    if value is None:
        return LogicalType.UNKNOWN
    if isinstance(value, bool):
        return LogicalType.BOOLEAN
    if isinstance(value, int):
        return LogicalType.INTEGER
    if isinstance(value, float):
        return LogicalType.FLOAT
    if isinstance(value, str):
        return LogicalType.STRING
    raise InvalidTransformError(f"Unsupported literal {value!r}.", field="expression")
