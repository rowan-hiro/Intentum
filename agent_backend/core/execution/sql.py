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
    TryCastExpr,
    ColumnExpr,
    DeriveStep,
    Expr,
    FilterStep,
    FunctionExpr,
    InExpr,
    JoinStep,
    LimitStep,
    LiteralExpr,
    RawQueryStep,
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
    def compile(self, ir: TransformIR, physical_inputs: dict[str, str], base: str | None = None) -> str:
        """The transform as one statement; after a raw_query, over ``base``, the relation holding its result.

        A raw_query is never compiled here: it runs as written in its sandbox
        (storage/duckdb/sandbox.py), and the steps after it read its result
        under the field names the IR gives its columns.
        """
        ctes: list[str] = []
        steps = list(enumerate(ir.steps, start=1))
        if ir.steps and isinstance(ir.steps[0], RawQueryStep):
            if base is None:
                raise TypeError("a raw_query step is compiled over the relation holding its result")
            query = ir.steps[0]
            columns = [q(c) if c == f.name else f"{q(c)} AS {q(f.name)}" for c, f in zip(query.columns, query.output_schema)]
            current = f"SELECT {', '.join(columns)} FROM {base}"
            steps = [(index - 1, step) for index, step in steps[1:]]
        else:
            current = f"SELECT {', '.join(q(f.name) for f in ir.input_schema)} FROM {q(physical_inputs[ir.source.dataset_id])}"
        ctes.append(f"s0 AS ({current})")
        prev = "s0"
        for index, step in steps:
            name = f"s{index}"
            body = self._step_sql(step, prev, physical_inputs)
            ctes.append(f"{name} AS ({body})")
            prev = name
        return "WITH " + ",\n".join(ctes) + f"\nSELECT * FROM {prev}"

    def compile_ties(self, ir: TransformIR, physical_inputs: dict[str, str]) -> str | None:
        """A statement counting the rows that tie on the keys of the sort that sets the output order, or None.

        That sort is the last one, followed only by steps that keep its rows and order (select, derive, rename)
        and at most one limit; with a limit, only tied groups whose key values reach into the rows it keeps count.
        The statement returns at most one row: the largest tied group's key values, its size, the number of tied
        groups and the number of tied rows. A transform that starts with a raw_query is not counted.
        """
        if not ir.steps or isinstance(ir.steps[0], RawQueryStep):
            return None
        position: int | None = None
        limit: LimitStep | None = None
        for index in range(len(ir.steps) - 1, -1, -1):
            step = ir.steps[index]
            if isinstance(step, SortStep):
                position = index
                break
            if isinstance(step, LimitStep) and limit is None:
                limit = step
                continue
            if not isinstance(step, (SelectStep, DeriveStep, RenameStep)):
                return None
        if position is None:
            return None
        sort = ir.steps[position]
        assert isinstance(sort, SortStep)
        sorted_ir = ir.model_copy(update={"steps": ir.steps[: position + 1]})
        chain = self.compile(sorted_ir, physical_inputs)
        prefix, _, _ = chain.rpartition("\nSELECT * FROM ")
        relation = f"s{position + 1}"
        keys = [q(k.field.name) for k in sort.keys]
        listed = ", ".join(keys)
        ctes = [f"tied AS (SELECT {listed}, count(*) AS n FROM {relation} GROUP BY {listed} HAVING count(*) > 1)"]
        kept = ""
        if limit is not None:
            ordering = ", ".join(f"{q(k.field.name)} {k.direction.upper()}" for k in sort.keys)
            offset = f" OFFSET {limit.offset}" if limit.offset else ""
            ctes.append(f"kept AS (SELECT {listed} FROM {relation} ORDER BY {ordering} LIMIT {limit.limit}{offset})")
            matched = " AND ".join(f"kept.{k} IS NOT DISTINCT FROM tied.{k}" for k in keys)
            kept = f" WHERE EXISTS (SELECT 1 FROM kept WHERE {matched})"
        return (prefix + ",\n" + ",\n".join(ctes)
                + f"\nSELECT {listed}, n, count(*) OVER (), sum(n) OVER () FROM tied{kept} ORDER BY n DESC, {listed} LIMIT 1")

    def compile_join_facts(self, ir: TransformIR, physical_inputs: dict[str, str], position: int,
                           sample: int = 5) -> tuple[str, str]:
        """Two statements describing what the join at ``position`` does to its inputs.

        The left input is the relation the transform holds just before the join; the right is the joined
        dataset's version. Both are grouped by the join keys. Each left group is then counted once, with the rows
        of every right group its keys equal under the join's own comparison: a key coerced for the comparison can
        equal several right groups (a float left key and two integer right keys that round to it), and those
        matches add up. A null key never matches. The first statement returns one row of counts: left rows, left
        rows with a null key, unmatched left rows, distinct non-null left keys, matched left keys, left keys
        matching several right rows, the rows those extra matches add, matched row pairs, right rows and
        unmatched right rows. The second returns at most ``sample`` unmatched left keys and ``sample`` left keys
        with several right matches: a tag, the key values, the left rows and the right rows of each.
        """
        step = ir.steps[position]
        if not isinstance(step, JoinStep) or (ir.steps and isinstance(ir.steps[0], RawQueryStep)):
            raise TypeError("join facts are compiled for a semantic join step of a transform without a raw_query")
        chain = self.compile(ir.model_copy(update={"steps": ir.steps[:position]}), physical_inputs)
        prefix, _, _ = chain.rpartition("\nSELECT * FROM ")
        keys = [f"k{i}" for i in range(len(step.on))]
        left = ", ".join(f"l.{q(c.left.name)} AS {k}" for c, k in zip(step.on, keys))
        right = ", ".join(f"r.{q(c.right.name)} AS {k}" for c, k in zip(step.on, keys))
        listed = ", ".join(keys)
        grouped = ", ".join(f"jl.{k}" for k in keys)
        null = " OR ".join(f"jl.{k} IS NULL" for k in keys)
        on = " AND ".join(f"jl.{k} = jr.{k}" for k in keys)
        ctes = (prefix + ",\n"
                + f"jl AS (SELECT {left}, count(*) AS n FROM s{position} AS l GROUP BY {listed}),\n"
                + f"jr AS (SELECT {right}, count(*) AS n FROM {q(physical_inputs[step.right.dataset_id])} AS r "
                  f"GROUP BY {listed}),\n"
                + f"jm AS (SELECT {', '.join(f'jl.{k} AS {k}' for k in keys)}, jl.n AS ln, sum(jr.n) AS rn, "
                  f"({null}) AS lnull FROM jl LEFT JOIN jr ON {on} GROUP BY {grouped}, jl.n),\n"
                + f"ju AS (SELECT jr.n FROM jr WHERE NOT EXISTS (SELECT 1 FROM jl WHERE {on}))")
        counts = (ctes + "\nSELECT coalesce(sum(ln), 0), coalesce(sum(ln) FILTER (WHERE lnull), 0), "
                  "coalesce(sum(ln) FILTER (WHERE rn IS NULL), 0), count(*) FILTER (WHERE NOT lnull), "
                  "count(*) FILTER (WHERE rn IS NOT NULL), count(*) FILTER (WHERE rn > 1), "
                  "coalesce(sum(ln * (rn - 1)) FILTER (WHERE rn > 1), 0), "
                  "coalesce(sum(ln * rn) FILTER (WHERE rn IS NOT NULL), 0), "
                  "(SELECT coalesce(sum(n), 0) FROM jr), (SELECT coalesce(sum(n), 0) FROM ju) FROM jm")
        samples = (ctes + ",\n"
                   + f"unmatched AS (SELECT 'unmatched' AS tag, {listed}, ln, rn FROM jm WHERE NOT lnull "
                     f"AND rn IS NULL ORDER BY ln DESC, {listed} LIMIT {int(sample)}),\n"
                   + f"multiple AS (SELECT 'multiple' AS tag, {listed}, ln, rn FROM jm WHERE rn > 1 "
                     f"ORDER BY rn DESC, {listed} LIMIT {int(sample)})\n"
                   + f"SELECT * FROM (SELECT * FROM unmatched UNION ALL SELECT * FROM multiple) "
                     f"ORDER BY tag DESC, CASE WHEN tag = 'unmatched' THEN ln ELSE rn END DESC, {listed}")
        return counts, samples

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
        if isinstance(e, TryCastExpr):
            return f"TRY_CAST({self.expr(e.expr)} AS {logical_to_physical(e.logical_type)})"
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
