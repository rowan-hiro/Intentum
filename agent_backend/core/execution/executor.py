"""Execute a plan against the analytics engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ExecutionFailedError
from ..ir import JoinStep, OutputMode, RawQueryStep, TransformIR
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
    distinct_keys: int | None = None  # distinct combinations of the columns asked for, when asked
    # Rows tied on the keys of the sort that sets the output order: (largest group's key values, its size,
    # tied groups, tied rows); None when nothing ties or nothing was counted.
    ties: tuple[list[Any], int, int, int] | None = None
    # What each semantic join step did to its inputs, in step order; empty when nothing was diagnosed.
    joins: list["JoinFacts"] = field(default_factory=list)


@dataclass
class JoinFacts:
    """What one semantic join step did to its inputs: data facts, not a judgement of the join.

    Counted over the relation the transform held just before the join (the left input) and the joined
    dataset's version (the right input), matched with the join's own key equality, so a null key never matches.
    """

    step: JoinStep
    position: int  # the join's index among the transform's steps
    left_rows: int
    null_key_left_rows: int
    unmatched_left_rows: int  # including the rows with a null key
    left_keys: int  # distinct non-null key values of the left input
    matched_left_keys: int
    multiple_match_keys: int  # left keys that match more than one right row
    added_rows: int  # rows the extra matches of those keys add
    matched_pairs: int
    right_rows: int
    unmatched_right_rows: int
    unmatched_sample: list[tuple[list[Any], int]] = field(default_factory=list)  # (key values, left rows)
    multiple_sample: list[tuple[list[Any], int, int]] = field(default_factory=list)  # (key values, left, right rows)

    @property
    def matched_left_rows(self) -> int:
        return self.left_rows - self.unmatched_left_rows

    @property
    def unmatched_left_keys(self) -> int:
        return self.left_keys - self.matched_left_keys

    @property
    def rows_out(self) -> int:
        """The rows the join step produces, by how it keeps unmatched rows."""
        kept_left = self.unmatched_left_rows if self.step.how in ("left", "full") else 0
        kept_right = self.unmatched_right_rows if self.step.how in ("right", "full") else 0
        return self.matched_pairs + kept_left + kept_right

    @property
    def coverage(self) -> float | None:
        """The share of left rows that matched at least one right row."""
        return round(self.matched_left_rows / self.left_rows, 4) if self.left_rows else None

    def key(self, values: list[Any]) -> dict[str, Any]:
        return {c.left.name: v for c, v in zip(self.step.on, values)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": f"transform.steps[{self.position}] (join)",
            "right": {"id": self.step.right.dataset_id, "name": self.step.right.name, "version": self.step.right.version},
            "how": self.step.how,
            "on": {c.left.name: c.right.name for c in self.step.on},
            "left_rows": self.left_rows,
            "matched_left_rows": self.matched_left_rows,
            "unmatched_left_rows": self.unmatched_left_rows,
            "null_key_left_rows": self.null_key_left_rows,
            "left_keys": self.left_keys,
            "matched_left_keys": self.matched_left_keys,
            "unmatched_left_keys": self.unmatched_left_keys,
            "left_keys_with_multiple_matches": self.multiple_match_keys,
            "rows_added_by_multiple_matches": self.added_rows,
            "right_rows": self.right_rows,
            "unmatched_right_rows": self.unmatched_right_rows,
            "rows_out": self.rows_out,
            "match_coverage": self.coverage,
            "unmatched_left_key_sample": [{"key": self.key(values), "left_rows": n}
                                          for values, n in self.unmatched_sample],
            "multiple_match_sample": [{"key": self.key(values), "left_rows": n, "right_rows": m}
                                      for values, n, m in self.multiple_sample],
        }


class Executor:
    def __init__(self, engine: AnalyticsEngine, compiler: SqlCompiler | None = None,
                 queries: QueryEngine | None = None) -> None:
        self.engine = engine
        self.compiler = compiler or SqlCompiler()
        self.queries = queries  # the sandbox raw_query statements run in

    def execute_transform(self, ir: TransformIR, plan: ExecutionPlan,
                          count_distinct: list[str] | None = None, diagnose_joins: bool = False) -> ExecutionResult:
        """Run the transform; ``count_distinct`` names columns whose distinct combinations the result should count.

        ``diagnose_joins`` counts what each semantic join step did to its inputs (``JoinFacts``). A transform that
        starts with a raw_query is not diagnosed: its later steps read a result that exists only in the sandbox.
        """
        if ir.steps and isinstance(ir.steps[0], RawQueryStep):
            return self._execute_after_query(ir, plan, count_distinct)
        result = self._execute(ir, plan, self.compiler.compile(ir, plan.physical_inputs), count_distinct)
        if result.row_count:  # one row kept by a limit can still be one of several tied rows
            try:
                result.ties = self._ties(ir, plan)
            except Exception:  # the count serves advice only: it never fails the transform
                result.ties = None
        if diagnose_joins:
            for position, step in enumerate(ir.steps):
                if not isinstance(step, JoinStep):
                    continue
                try:
                    result.joins.append(self._join_facts(ir, plan, position, step))
                except Exception:  # the facts serve the response only: they never fail the transform
                    continue
        return result

    def _join_facts(self, ir: TransformIR, plan: ExecutionPlan, position: int, step: JoinStep) -> JoinFacts:
        counts_sql, samples_sql = self.compiler.compile_join_facts(ir, plan.physical_inputs, position)
        _, rows = self.engine.query(counts_sql)
        counts = [int(v or 0) for v in rows[0]]
        facts = JoinFacts(step, position, *counts)
        if facts.unmatched_left_rows > facts.null_key_left_rows or facts.multiple_match_keys:
            width = len(step.on)
            _, rows = self.engine.query(samples_sql)
            for tag, *values in rows:
                keys, left, right = list(values[:width]), int(values[width]), values[width + 1]
                if tag == "unmatched":
                    facts.unmatched_sample.append((keys, left))
                else:
                    facts.multiple_sample.append((keys, left, int(right)))
        return facts

    def _ties(self, ir: TransformIR, plan: ExecutionPlan) -> tuple[list[Any], int, int, int] | None:
        sql = self.compiler.compile_ties(ir, plan.physical_inputs)
        if sql is None:
            return None
        _, rows = self.engine.query(sql)
        if not rows:
            return None
        *keys, size, groups, tied = rows[0]
        return list(keys), int(size), int(groups), int(tied)

    def _execute(self, ir: TransformIR, plan: ExecutionPlan, sql: str,
                 count_distinct: list[str] | None = None) -> ExecutionResult:
        if ir.output.mode == OutputMode.MATERIALIZED:
            assert plan.output_table is not None
            row_count = self.engine.create_table_from_query(plan.output_table, sql)
            columns, rows = self.engine.sample(plan.output_table, ir.output.preview_limit)
            result = ExecutionResult(columns=columns, rows=[list(r) for r in rows], row_count=row_count, sql=sql,
                                     physical_table=plan.output_table, truncated=row_count > len(rows))
            if count_distinct and row_count:
                result.distinct_keys = self._count(lambda: self.engine.count_distinct_rows(plan.output_table, count_distinct))
            return result
        row_count = self.engine.count_rows_of_query(sql)
        columns, rows = self.engine.query(sql, limit=ir.output.preview_limit)
        result = ExecutionResult(columns=columns, rows=[list(r) for r in rows], row_count=row_count, sql=sql,
                                 truncated=row_count > len(rows))
        if count_distinct and row_count:
            # A preview has no table, and after a raw_query its SQL runs only while the sandbox session is open.
            result.distinct_keys = self._count(lambda: self.engine.count_distinct_of_query(sql, count_distinct))
        return result

    @staticmethod
    def _count(count: Any) -> int | None:
        """The count serves advice only: when it fails the transform still succeeds, and no match is promised."""
        try:
            return count()
        except ExecutionFailedError:
            return None

    def _execute_after_query(self, ir: TransformIR, plan: ExecutionPlan,
                             count_distinct: list[str] | None = None) -> ExecutionResult:
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
                result = self._execute(ir, plan, self.compiler.compile(ir, plan.physical_inputs, base=base), count_distinct)
            except ExecutionFailedError as err:
                if "sql" in err.details:
                    err.details = {**err.details, "sql": shown}
                raise
        result.sql = shown
        return result

    def rollback_table(self, table: str | None) -> None:
        if table:
            self.engine.drop_table(table)
