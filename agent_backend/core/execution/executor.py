"""Execute a plan against the analytics engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..ir import OutputMode, TransformIR
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
    def __init__(self, engine: AnalyticsEngine, compiler: SqlCompiler | None = None) -> None:
        self.engine = engine
        self.compiler = compiler or SqlCompiler()

    def execute_transform(self, ir: TransformIR, plan: ExecutionPlan) -> ExecutionResult:
        sql = self.compiler.compile(ir, plan.physical_inputs)
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

    def rollback_table(self, table: str | None) -> None:
        if table:
            self.engine.drop_table(table)
