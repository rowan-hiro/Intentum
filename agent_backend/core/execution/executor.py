"""Execute a plan against the analytics engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ExecutionFailedError
from ..ir import OutputMode, RawQueryStep, TransformIR
from ..ir.raw_query import QueryEngine
from ..planner import ExecutionPlan
from ...storage.duckdb.engine import AnalyticsEngine
from .sql import SqlCompiler


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    sql: str
    physical_table: str | None = None
    truncated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class Executor:
    def __init__(self, engine: AnalyticsEngine, compiler: SqlCompiler | None = None,
                 queries: QueryEngine | None = None) -> None:
        self.engine = engine
        self.compiler = compiler or SqlCompiler()
        self.queries = queries  # the sandbox raw_query statements run in

    def execute_transform(self, ir: TransformIR, plan: ExecutionPlan) -> ExecutionResult:
        if ir.steps and isinstance(ir.steps[0], RawQueryStep):
            return self._execute_after_query(ir, plan)
        return self._execute(ir, plan, self.compiler.compile(ir, plan.physical_inputs))

    def _execute(self, ir: TransformIR, plan: ExecutionPlan, sql: str) -> ExecutionResult:
        if ir.output.mode == OutputMode.MATERIALIZED:
            assert plan.output_table is not None
            row_count = self.engine.create_table_from_query(plan.output_table, sql)
            columns, rows = self.engine.sample(plan.output_table, ir.output.preview_limit)
            return ExecutionResult(columns=columns, rows=[list(r) for r in rows], row_count=row_count, sql=sql,
                                   physical_table=plan.output_table, truncated=row_count > len(rows))
        row_count = self.engine.count_rows_of_query(sql)
        columns, rows = self.engine.query(sql, limit=ir.output.preview_limit)
        return ExecutionResult(columns=columns, rows=[list(r) for r in rows], row_count=row_count, sql=sql,
                               truncated=row_count > len(rows))

    def _execute_after_query(self, ir: TransformIR, plan: ExecutionPlan) -> ExecutionResult:
        """Run the raw_query in its sandbox, then the rest of the transform over its result as usual."""
        query = ir.steps[0]
        assert isinstance(query, RawQueryStep)
        if self.queries is None:
            raise ExecutionFailedError("raw_query is not available: this backend's engine has no query sandbox.")
        inputs = {b.placeholder: plan.physical_inputs[b.dataset.dataset_id] for b in query.inputs}
        # What explain and errors show: the statement as written and the engine's SQL over its result, without the
        # sandbox's own names.
        bound = ", ".join(f"{b.placeholder} = {b.dataset.name} v{b.dataset.version}" for b in query.inputs)
        shown = (f"-- raw_query, run in a read-only sandbox holding {bound}\n{query.sql.strip()}\n"
                 f"-- then, over its result as raw_query:\n"
                 f"{self.compiler.compile(ir, plan.physical_inputs, base='raw_query')}")
        with self.queries.session(inputs) as session:
            # DESCRIBE once more over the real inputs: the IR's schema is what the statement returns.
            described = session.describe(query.sql)
            if ([name for name, _, _ in described] != list(query.columns)
                    or [logical for _, _, logical in described] != [f.logical_type for f in query.output_schema]):
                raise ExecutionFailedError("The raw_query statement returns a different shape over its inputs than it "
                                           "did when it was validated.", details={"raw_query": {"refused": "execution"}})
            base = session.run(query.sql)
            try:
                result = self._execute(ir, plan, self.compiler.compile(ir, plan.physical_inputs, base=base))
            except ExecutionFailedError as err:
                if "sql" in err.details:
                    err.details = {**err.details, "sql": shown}
                raise
        result.sql = shown
        return result

    def rollback_table(self, table: str | None) -> None:
        if table:
            self.engine.drop_table(table)
