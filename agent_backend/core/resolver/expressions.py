"""Resolve loose expression trees (or expression strings) into typed canonical
expressions against a scope."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..errors import InvalidTransformError, TypeMismatchError
from ..ir import (
    BinaryExpr,
    CastExpr,
    TryCastExpr,
    ColumnExpr,
    Expr,
    FunctionExpr,
    InExpr,
    LiteralExpr,
    UnaryExpr,
)
from ..ir.expression_parser import _KEYWORDS, parse_expression
from ..ir.typing import (
    COMPARISON_OPS,
    FUNCTIONS,
    TEMPORAL,
    arguments_are_swapped,
    binary_result_type,
    comparable,
    function_result_type,
    literal_type,
    signature,
    unary_result_type,
    validate_function_arguments,
)
from ..models.entities import LogicalType
from .common import ResolutionNote, pick
from .fields import FieldResolver, Scope

_FILTER_OP_ALIASES = {
    "eq": "=", "==": "=", "=": "=", "equals": "=", "is": "=",
    "ne": "!=", "neq": "!=", "!=": "!=", "<>": "!=", "not_equals": "!=",
    "gt": ">", ">": ">", "greater_than": ">",
    "gte": ">=", ">=": ">=", "ge": ">=",
    "lt": "<", "<": "<", "less_than": "<",
    "lte": "<=", "<=": "<=", "le": "<=",
    "in": "in", "not_in": "not_in", "nin": "not_in",
    "contains": "contains", "like": "contains", "starts_with": "starts_with", "ends_with": "ends_with",
    "is_null": "is_null", "null": "is_null", "not_null": "not_null", "is_not_null": "not_null",
    "between": "between",
}


class ExpressionResolver:
    def __init__(self, fields: FieldResolver) -> None:
        self.fields = fields

    def resolve(
        self,
        loose: Any,
        scope: Scope,
        *,
        field: str,
        notes: list[ResolutionNote] | None = None,
    ) -> Expr:
        if isinstance(loose, str):
            try:
                parsed = parse_expression(self._quote_special_fields(loose, scope))
            except InvalidTransformError as err:
                # The parser knows the text; the resolver knows where it sits and what the language accepts.
                if err.field == "expression":
                    err.field = field
                err.details.update({"allowed_functions": sorted(FUNCTIONS), "keywords": sorted(_KEYWORDS),
                                    "allowed_operators": sorted(set(_FILTER_OP_ALIASES) | {"+", "-", "*", "/", "%", "and", "or"})})
                raise
            return self.resolve(parsed, scope, field=field, notes=notes)
        if isinstance(loose, bool) or loose is None or isinstance(loose, (int, float)):
            return LiteralExpr(value=loose, logical_type=literal_type(loose))
        if isinstance(loose, list):
            parts = [self.resolve(item, scope, field=field, notes=notes) for item in loose]
            return self._combine("and", parts)
        if not isinstance(loose, dict):
            raise InvalidTransformError(f"Unsupported expression {loose!r} in {field}.", field=field)

        if "column" in loose and len(loose) == 1:
            return self._column(loose["column"], scope, field, notes)
        if "value" in loose and len(loose) == 1:
            return LiteralExpr(value=loose["value"], logical_type=literal_type(loose["value"]))
        if "literal" in loose and len(loose) == 1:
            return LiteralExpr(value=loose["literal"], logical_type=literal_type(loose["literal"]))
        if "and" in loose:
            return self._combine("and", [self.resolve(p, scope, field=field, notes=notes) for p in loose["and"]])
        if "or" in loose:
            return self._combine("or", [self.resolve(p, scope, field=field, notes=notes) for p in loose["or"]])
        if "not" in loose:
            operand = self.resolve(loose["not"], scope, field=field, notes=notes)
            return UnaryExpr(op="not", operand=operand, logical_type=unary_result_type("not", operand.logical_type))
        if "neg" in loose:
            operand = self.resolve(loose["neg"], scope, field=field, notes=notes)
            return UnaryExpr(op="neg", operand=operand, logical_type=unary_result_type("neg", operand.logical_type))
        if "function" in loose or "fn" in loose:
            name = str(pick(loose, "function", "fn")).lower()
            args = [self.resolve(a, scope, field=field, notes=notes) for a in loose.get("args", [])]
            if arguments_are_swapped(name, [a.logical_type for a in args]):
                # The call fits its signature the other way round; the IR keeps one order.
                args = [args[1], args[0]]
                if notes is not None:
                    notes.append(ResolutionNote(field, f"{name}({args[1].logical_type}, {args[0].logical_type})",
                                                signature(name), "arguments reordered to the function's signature"))
            validate_function_arguments(name, args)
            return FunctionExpr(name=name, args=args, logical_type=function_result_type(name, [a.logical_type for a in args]))
        if "cast" in loose or "try_cast" in loose:
            spelling = "cast" if "cast" in loose else "try_cast"
            target = self._parse_type(loose.get("to") or loose.get("type"))
            inner = self.resolve(loose[spelling], scope, field=field, notes=notes)
            node = CastExpr if spelling == "cast" else TryCastExpr
            return node(expr=inner, logical_type=target)
        if "op" in loose and ("left" in loose or "field" in loose or "column" in loose):
            return self._binary(loose, scope, field, notes)
        if ("field" in loose or "column" in loose) and "value" in loose:
            return self._binary({**loose, "op": loose.get("op", "=")}, scope, field, notes)
        if len(loose) == 1:
            # {"region": "West"} shorthand for equality
            (name, value), = loose.items()
            return self._binary({"field": name, "op": "=", "value": value}, scope, field, notes)
        raise InvalidTransformError(
            f"Cannot interpret expression {loose!r} in {field}.",
            field=field,
            hint="Use an expression string such as \"amount > 100 and region = 'West'\" or "
                 "an object with field/op/value.",
        )

    # -- internals -------------------------------------------------------
    @staticmethod
    def _quote_special_fields(text: str, scope: Scope) -> str:
        """Double-quote bare occurrences of field names that are not plain identifiers.

        Real column names such as ``募集资金总额(元)`` or ``Unit Price`` would
        otherwise be read as a function call or two identifiers. Names are
        matched longest-first in one pass. Existing quoted tokens, including
        doubled quote escapes, are preserved without inspecting their contents.
        """
        import re

        special = sorted(
            (f.name for f in scope.fields if '"' not in f.name and not re.fullmatch(r"[^\W\d]\w*", f.name)),
            key=len, reverse=True,
        )
        if not special:
            return text
        quoted = r"""(?:'(?:[^']|'')*'|"(?:[^"]|"")*")"""
        fields = r'(?<!["\w])(?:' + "|".join(re.escape(name) for name in special) + r')(?!["\w])'
        pattern = re.compile(f"(?P<quoted>{quoted})|(?P<field>{fields})")
        return pattern.sub(
            lambda match: match.group(0) if match.group("quoted") is not None else '"' + match.group(0) + '"',
            text,
        )

    def _column(self, name: Any, scope: Scope, field: str, notes: list[ResolutionNote] | None) -> ColumnExpr:
        resolved = self.fields.resolve(name, scope, field=field, notes=notes)
        return ColumnExpr(field=resolved.ref(), logical_type=resolved.logical_type)

    def _combine(self, op: str, parts: list[Expr]) -> Expr:
        if not parts:
            raise InvalidTransformError(f"Empty '{op}' expression.", field="filter")
        result = parts[0]
        for part in parts[1:]:
            result = BinaryExpr(
                op=op, left=result, right=part,
                logical_type=binary_result_type(op, result.logical_type, part.logical_type),
            )
        return result

    def _binary(self, loose: dict[str, Any], scope: Scope, field: str, notes: list[ResolutionNote] | None) -> Expr:
        raw_op = str(pick(loose, "op", "operator", default="=")).strip().lower()
        op = _FILTER_OP_ALIASES.get(raw_op, raw_op)
        if "left" in loose:
            left = self.resolve(loose["left"], scope, field=field, notes=notes)
        else:
            left = self._column(pick(loose, "field", "column"), scope, field, notes)
        # "right" (parser output) is an expression; "value"/"values" (object form) are literals.
        right_is_expr = "right" in loose
        right_loose = pick(loose, "right", "value", "values")

        if op == "is_null" or op == "not_null":
            node: Expr = FunctionExpr(name="is_null", args=[left], logical_type=LogicalType.BOOLEAN)
            return node if op == "is_null" else UnaryExpr(op="not", operand=node, logical_type=LogicalType.BOOLEAN)
        if op in ("in", "not_in"):
            if not isinstance(right_loose, list) or not right_loose:
                raise InvalidTransformError(f"Operator {op!r} requires a non-empty list of values.", field=field)
            values = [self._literal_for(left, v, field) for v in right_loose]
            return InExpr(expr=left, values=values, negated=(op == "not_in"))
        if op == "between":
            if not isinstance(right_loose, list) or len(right_loose) != 2:
                raise InvalidTransformError("Operator 'between' requires [low, high].", field=field)
            low = self._coerce(left, self._right_operand(right_loose[0], right_is_expr, scope, field, notes))
            high = self._coerce(left, self._right_operand(right_loose[1], right_is_expr, scope, field, notes))
            ge = BinaryExpr(op=">=", left=left, right=low, logical_type=self._result_type(">=", left, low, scope, field))
            le = BinaryExpr(op="<=", left=left, right=high, logical_type=self._result_type("<=", left, high, scope, field))
            return BinaryExpr(op="and", left=ge, right=le, logical_type=LogicalType.BOOLEAN)
        if op in ("contains", "starts_with", "ends_with"):
            right = self._right_operand(right_loose, right_is_expr, scope, field, notes)
            return FunctionExpr(name=op, args=[left, right], logical_type=function_result_type(op, [left.logical_type, right.logical_type]))
        if op not in COMPARISON_OPS and op not in ("+", "-", "*", "/", "%", "and", "or"):
            raise InvalidTransformError(
                f"Unknown operator {raw_op!r}.",
                field=field,
                details={"allowed_operators": sorted(set(_FILTER_OP_ALIASES) | {"+", "-", "*", "/", "%", "and", "or"})},
            )
        right = self._right_operand(right_loose, right_is_expr, scope, field, notes)
        if op in COMPARISON_OPS:
            if isinstance(right, LiteralExpr) and right.value is None:
                raise TypeMismatchError(
                    "Comparing with null is never true; use is_null / not_null instead.",
                    field=field,
                    hint="Example: {\"field\": \"region\", \"op\": \"is_null\"}",
                )
            right = self._coerce(left, right)
            left = self._coerce(right, left)
        return BinaryExpr(op=op, left=left, right=right, logical_type=self._result_type(op, left, right, scope, field))

    @staticmethod
    def _operand_facts(expr: Expr) -> dict[str, Any]:
        """One operand as the agent wrote it: its text, its type, and whether it is a literal."""
        if isinstance(expr, ColumnExpr):
            text = expr.field.name
        elif isinstance(expr, LiteralExpr):
            text = repr(expr.value)
        elif isinstance(expr, FunctionExpr):
            text = f"{expr.name}(...)"
        else:
            text = "expression"
        return {"text": text, "type": str(expr.logical_type), "literal": isinstance(expr, LiteralExpr)}

    def _result_type(self, op: str, left: Expr, right: Expr, scope: Scope, field: str) -> LogicalType:
        """binary_result_type, with a refusal that names the step, both operands and the comparable fields."""
        try:
            return binary_result_type(op, left.logical_type, right.logical_type)
        except TypeMismatchError as err:
            if err.field == "expression":
                err.field = field
            literal = right if isinstance(right, LiteralExpr) else left if isinstance(left, LiteralExpr) else None
            err.details.update({"operator": op, "left": self._operand_facts(left), "right": self._operand_facts(right)})
            if literal is not None:
                err.details["comparable_fields"] = [f.name for f in scope.fields if comparable(f.logical_type, literal.logical_type)]
            raise

    def _right_operand(self, loose: Any, is_expr: bool, scope: Scope, field: str, notes: list[ResolutionNote] | None) -> Expr:
        if loose is None and not is_expr:
            raise InvalidTransformError("Comparison needs a 'value'.", field=field)
        if is_expr or isinstance(loose, dict):
            return self.resolve(loose, scope, field=field, notes=notes)
        if isinstance(loose, list):
            raise InvalidTransformError("A list value only works with the 'in', 'not_in' or 'between' operators.", field=field)
        return LiteralExpr(value=loose, logical_type=literal_type(loose))

    def _literal_for(self, left: Expr, value: Any, field: str) -> LiteralExpr:
        if isinstance(value, dict) and "value" in value:
            value = value["value"]
        literal = LiteralExpr(value=value, logical_type=literal_type(value))
        coerced = self._coerce(left, literal)
        if isinstance(coerced, CastExpr):
            return LiteralExpr(value=value, logical_type=coerced.logical_type)
        if not _comparable(left.logical_type, literal.logical_type):
            raise TypeMismatchError(
                f"Value {value!r} ({literal.logical_type}) is not comparable with {left.logical_type}.",
                field=field,
            )
        return literal

    @staticmethod
    def _coerce(anchor: Expr, other: Expr) -> Expr:
        """Coerce string literals to the anchor's temporal type when they parse as dates."""
        if anchor.logical_type in TEMPORAL and isinstance(other, LiteralExpr) and isinstance(other.value, str):
            text = other.value
            try:
                if anchor.logical_type == LogicalType.DATE:
                    date.fromisoformat(text)
                else:
                    datetime.fromisoformat(text)
            except ValueError as exc:
                raise TypeMismatchError(
                    f"{text!r} is not a valid ISO {anchor.logical_type} literal.", field="expression"
                ) from exc
            return CastExpr(expr=other, logical_type=anchor.logical_type)
        return other

    @staticmethod
    def _parse_type(value: Any) -> LogicalType:
        text = str(value).strip().lower()
        target = _SQL_TYPE_NAMES.get(text)
        if target is None:
            try:
                target = LogicalType(text)
            except ValueError:
                target = None
        if target is None:
            accepted = [str(t) for t in LogicalType]
            raise InvalidTransformError(
                f"Unknown type {value!r}; expected one of {accepted}, or a SQL name such as int, double, varchar, "
                "bool or datetime.", field="cast", details={"received": value, "accepted": accepted},
            )
        return target


# SQL spellings agents write for the logical types a cast can target.
_SQL_TYPE_NAMES: dict[str, LogicalType] = {
    **{name: LogicalType.INTEGER for name in ("int", "int2", "int4", "int8", "smallint", "tinyint", "bigint", "hugeint")},
    **{name: LogicalType.FLOAT for name in ("double", "real", "decimal", "numeric", "float4", "float8")},
    **{name: LogicalType.STRING for name in ("varchar", "text", "char", "str")},
    "bool": LogicalType.BOOLEAN,
    "datetime": LogicalType.TIMESTAMP,
}


def _comparable(left: LogicalType, right: LogicalType) -> bool:
    from ..ir.typing import comparable

    return comparable(left, right)
