"""Compile a canonical ``TransformIR`` into DuckDB SQL.

SQL is an internal execution IR only. Every identifier comes from validated
canonical fields and every function from the allowlist, so the emitted SQL is
fully determined by the IR.
"""

from __future__ import annotations

from ..ir import (
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
    LimitStep,
    LiteralExpr,
    RenameStep,
    SemiJoinStep,
    SelectStep,
    SortStep,
    TransformIR,
    UnaryExpr,
)
from ..ir.typing import FUNCTIONS
from ...storage.duckdb.engine import logical_to_physical, quote_ident as q


class SqlCompiler:
    def compile(self, ir: TransformIR, physical_inputs: dict[str, str]) -> str:
        ctes: list[str] = []
        current = f"SELECT {', '.join(q(f.name) for f in ir.input_schema)} FROM {q(physical_inputs[ir.source.dataset_id])}"
        ctes.append(f"s0 AS ({current})")
        prev = "s0"
        for index, step in enumerate(ir.steps, start=1):
            name = f"s{index}"
            body = self._step_sql(step, prev, physical_inputs)
            ctes.append(f"{name} AS ({body})")
            prev = name
        return "WITH " + ",\n".join(ctes) + f"\nSELECT * FROM {prev}"

    def _step_sql(self, step, prev: str, physical_inputs: dict[str, str]) -> str:
        if isinstance(step, SelectStep):
            return f"SELECT {', '.join(q(f.name) for f in step.fields)} FROM {prev}"
        if isinstance(step, FilterStep):
            return f"SELECT * FROM {prev} WHERE {self.expr(step.predicate)}"
        if isinstance(step, AggregateStep):
            parts = [q(f.name) for f in step.group_by]
            for m in step.measures:
                parts.append(f"{self._agg(m.function, m.field.name if m.field else None)} AS {q(m.alias)}")
            group = f" GROUP BY {', '.join(q(f.name) for f in step.group_by)}" if step.group_by else ""
            return f"SELECT {', '.join(parts)} FROM {prev}{group}"
        if isinstance(step, SortStep):
            keys = ", ".join(f"{q(k.field.name)} {k.direction.upper()}" for k in step.keys)
            return f"SELECT * FROM {prev} ORDER BY {keys}"
        if isinstance(step, LimitStep):
            offset = f" OFFSET {step.offset}" if step.offset else ""
            return f"SELECT * FROM {prev} LIMIT {step.limit}{offset}"
        if isinstance(step, RenameStep):
            renamed = {m.field.name: m.to for m in step.mappings}
            current_names = [f.name for f in step.output_schema]
            # output_schema already holds the renamed names, in order; recover originals
            originals = list(renamed.items())
            reverse = {new: old for old, new in originals}
            cols = [f"{q(reverse[n])} AS {q(n)}" if n in reverse else q(n) for n in current_names]
            return f"SELECT {', '.join(cols)} FROM {prev}"
        if isinstance(step, DeriveStep):
            return f"SELECT *, {self.expr(step.expression)} AS {q(step.name)} FROM {prev}"
        if isinstance(step, JoinStep):
            right_table = physical_inputs[step.right.dataset_id]
            how = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}[step.how]
            on = " AND ".join(f"l.{q(c.left.name)} = r.{q(c.right.name)}" for c in step.on)
            left_cols = [f"l.{q(f.name)}" for f in step.output_schema[: len(step.output_schema) - len(step.right_fields)]]
            right_cols = [f"r.{q(jo.field.name)} AS {q(jo.output_name)}" for jo in step.right_fields]
            return f"SELECT {', '.join(left_cols + right_cols)} FROM {prev} AS l {how} {q(right_table)} AS r ON {on}"
        if isinstance(step, SemiJoinStep):
            right_table = physical_inputs[step.right.dataset_id]
            on = " AND ".join(f"l.{q(c.left.name)} = r.{q(c.right.name)}" for c in step.on)
            return f"SELECT l.* FROM {prev} AS l WHERE EXISTS (SELECT 1 FROM {q(right_table)} AS r WHERE {on})"
        raise TypeError(f"cannot compile step {type(step).__name__}")

    @staticmethod
    def _agg(function: AggregateFunction, column: str | None) -> str:
        if function == AggregateFunction.COUNT:
            return f"COUNT({q(column)})" if column else "COUNT(*)"
        if function == AggregateFunction.COUNT_DISTINCT:
            return f"COUNT(DISTINCT {q(column)})"
        return f"{function.upper()}({q(column)})"

    def expr(self, e: Expr) -> str:
        if isinstance(e, ColumnExpr):
            return q(e.field.name)
        if isinstance(e, LiteralExpr):
            return self._literal(e.value)
        if isinstance(e, BinaryExpr):
            op = {"and": "AND", "or": "OR", "!=": "<>"}.get(e.op, e.op)
            return f"({self.expr(e.left)} {op} {self.expr(e.right)})"
        if isinstance(e, UnaryExpr):
            return f"(NOT {self.expr(e.operand)})" if e.op == "not" else f"(-{self.expr(e.operand)})"
        if isinstance(e, FunctionExpr):
            spec = FUNCTIONS[e.name]
            args = [self.expr(a) for a in e.args]
            if spec.sql:
                return spec.sql.format(*args)
            return f"{spec.name}({', '.join(args)})"
        if isinstance(e, InExpr):
            values = ", ".join(self._literal(v.value) for v in e.values)
            return f"({self.expr(e.expr)} {'NOT IN' if e.negated else 'IN'} ({values}))"
        if isinstance(e, CastExpr):
            return f"CAST({self.expr(e.expr)} AS {logical_to_physical(e.logical_type)})"
        raise TypeError(f"cannot compile expression {type(e).__name__}")

    @staticmethod
    def _literal(value) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return repr(value)
        return "'" + str(value).replace("'", "''") + "'"
