"""Turn a validated canonical IR into an explicit, inspectable execution plan.

The plan is a linear list of physical-ish steps (Scan → Filter → HashAggregate
→ Materialize → RegisterLineage ...). There is no cost-based optimization; the
value of the plan is that it is explicit, logged and stored with the
operation, so a failed request can be inspected step by step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..ir import (
    AggregateStep,
    DeriveStep,
    FilterStep,
    JoinStep,
    LimitStep,
    OutputMode,
    RawQueryStep,
    RenameStep,
    SemiJoinStep,
    SelectStep,
    SortStep,
    TransformIR,
)
from ..models.entities import DatasetVersion


@dataclass
class PlanStep:
    kind: str
    description: str
    details: dict[str, Any] = field(default_factory=dict)
    ir_step_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"kind": self.kind, "description": self.description}
        if self.details:
            body["details"] = self.details
        if self.ir_step_index is not None:
            body["ir_step"] = self.ir_step_index
        return body


@dataclass
class ExecutionPlan:
    steps: list[PlanStep]
    physical_inputs: dict[str, str]  # dataset_id -> physical table
    output_table: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "physical_inputs": self.physical_inputs,
            "output_table": self.output_table,
        }

    def to_text(self) -> str:
        return " → ".join(s.description for s in self.steps)


class Planner:
    def plan_transform(
        self,
        ir: TransformIR,
        versions: dict[str, DatasetVersion],
        output_table: str | None,
        output_dataset_id: str | None,
    ) -> ExecutionPlan:
        source_table = versions[ir.source.dataset_id].physical_table
        steps = []
        if not (ir.steps and isinstance(ir.steps[0], RawQueryStep)):
            # A raw_query reads its inputs itself, in its sandbox; the plan starts with it instead.
            steps.append(PlanStep(
                "Scan",
                f"Scan({ir.source.name} v{ir.source.version})",
                {"dataset_id": ir.source.dataset_id, "table": source_table, "columns": [f.name for f in ir.input_schema]},
            ))
        for index, step in enumerate(ir.steps):
            steps.append(self._plan_step(step, index, versions))
        if ir.output.mode == OutputMode.MATERIALIZED:
            steps.append(
                PlanStep("Materialize", f"Materialize({ir.output.name} as {output_dataset_id})",
                         {"table": output_table, "dataset_id": output_dataset_id, "columns": [f.name for f in ir.output_schema]})
            )
            for ref in ir.referenced_datasets():
                steps.append(PlanStep("RegisterLineage", f"RegisterLineage({ref.dataset_id} → {output_dataset_id})",
                                      {"source": ref.dataset_id, "target": output_dataset_id}))
            steps.append(PlanStep("Audit", "Audit(dataset.created, version.created, lineage.recorded)"))
        else:
            steps.append(PlanStep("Preview", f"Preview(limit={ir.output.preview_limit})", {"columns": [f.name for f in ir.output_schema]}))
        inputs = {ref.dataset_id: versions[ref.dataset_id].physical_table for ref in ir.referenced_datasets()}
        return ExecutionPlan(steps=steps, physical_inputs=inputs, output_table=output_table)

    @staticmethod
    def _plan_step(step, index: int, versions: dict[str, DatasetVersion]) -> PlanStep:
        if isinstance(step, SelectStep):
            names = [f.name for f in step.fields]
            return PlanStep("Project", f"Project({', '.join(names)})", {"columns": names}, index)
        if isinstance(step, FilterStep):
            return PlanStep("Filter", f"Filter({_expr_text(step.predicate)})", {"predicate": _expr_text(step.predicate)}, index)
        if isinstance(step, AggregateStep):
            group = [f.name for f in step.group_by]
            measures = [f"{m.function.upper()}({m.field.name if m.field else '*'}) AS {m.alias}" for m in step.measures]
            return PlanStep("HashAggregate", f"HashAggregate({', '.join(group + measures)})",
                            {"group_by": group, "measures": measures}, index)
        if isinstance(step, SortStep):
            keys = [f"{k.field.name} {k.direction}" for k in step.keys]
            return PlanStep("Sort", f"Sort({', '.join(keys)})", {"keys": keys}, index)
        if isinstance(step, LimitStep):
            return PlanStep("Limit", f"Limit({step.limit}" + (f", offset={step.offset}" if step.offset else "") + ")",
                            {"limit": step.limit, "offset": step.offset}, index)
        if isinstance(step, RenameStep):
            pairs = [f"{m.field.name}→{m.to}" for m in step.mappings]
            return PlanStep("Rename", f"Rename({', '.join(pairs)})", {"mappings": pairs}, index)
        if isinstance(step, DeriveStep):
            return PlanStep("Derive", f"Derive({step.name} = {_expr_text(step.expression)})",
                            {"name": step.name, "expression": _expr_text(step.expression)}, index)
        if isinstance(step, JoinStep):
            on = [f"{c.left.name} = {c.right.name}" for c in step.on]
            return PlanStep("HashJoin", f"HashJoin({step.how}, {step.right.name} v{step.right.version} on {', '.join(on)})",
                            {"how": step.how, "right_table": versions[step.right.dataset_id].physical_table, "on": on}, index)
        if isinstance(step, SemiJoinStep):
            on = [f"{c.left.name} = {c.right.name}" for c in step.on]
            return PlanStep(
                "SemiJoin",
                f"SemiJoin({step.right.name} v{step.right.version} on {', '.join(on)})",
                {"right_table": versions[step.right.dataset_id].physical_table, "on": on},
                index,
            )
        if isinstance(step, RawQueryStep):
            bound = [f"{b.placeholder}={b.dataset.name} v{b.dataset.version}" for b in step.inputs]
            return PlanStep(
                "RawQuery",
                f"RawQuery({', '.join(bound)})",
                {"sql": step.sql,
                 "inputs": {b.placeholder: {"dataset_id": b.dataset.dataset_id, "name": b.dataset.name,
                                            "version": b.dataset.version} for b in step.inputs},
                 "columns": [f.name for f in step.output_schema],
                 "sandbox": "read-only; one table per placeholder; no file, network, extension or setting access"},
                index,
            )
        raise TypeError(f"unplannable step {type(step).__name__}")


def _expr_text(expr) -> str:
    kind = expr.kind
    if kind == "column":
        return expr.field.name
    if kind == "literal":
        return repr(expr.value) if isinstance(expr.value, str) else str(expr.value)
    if kind == "binary":
        return f"({_expr_text(expr.left)} {expr.op} {_expr_text(expr.right)})"
    if kind == "unary":
        return f"(not {_expr_text(expr.operand)})" if expr.op == "not" else f"(-{_expr_text(expr.operand)})"
    if kind == "function":
        return f"{expr.name}({', '.join(_expr_text(a) for a in expr.args)})"
    if kind == "in":
        return f"({_expr_text(expr.expr)} {'not in' if expr.negated else 'in'} ({', '.join(_expr_text(v) for v in expr.values)}))"
    if kind == "cast":
        return f"cast({_expr_text(expr.expr)} as {expr.logical_type})"
    return "?"
