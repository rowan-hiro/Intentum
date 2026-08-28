"""Resolve a loose transform intent into a canonical ``TransformIR``.

The resolver walks the requested steps in order, maintaining the relation
schema ("scope") that is current after each step, so that later steps can
reference fields produced by earlier ones (for example sorting by an
aggregate alias). Every lenient decision is recorded as a ``ResolutionNote``.
"""

from __future__ import annotations

from typing import Any

from ..errors import InvalidIntentError, InvalidTransformError
from ..ir import (
    AggregateFunction,
    AggregateStep,
    DatasetRef,
    DeriveStep,
    FilterStep,
    JoinCondition,
    JoinOutput,
    JoinStep,
    LimitStep,
    Measure,
    OutputMode,
    OutputSpec,
    RenameMapping,
    RenameStep,
    SelectStep,
    SortKey,
    SortStep,
    Step,
    TransformIR,
)
from ..ir.typing import aggregate_result_type
from ..models.entities import Dataset, LogicalType, SemanticRole
from .common import ResolutionNote, pick, reject_unknown_keys, slugify
from .datasets import DatasetResolver
from .expressions import ExpressionResolver
from .fields import FieldResolver, Scope, ScopeField

_AGG_ALIASES = {
    "sum": AggregateFunction.SUM, "total": AggregateFunction.SUM,
    "avg": AggregateFunction.AVG, "average": AggregateFunction.AVG, "mean": AggregateFunction.AVG,
    "min": AggregateFunction.MIN, "minimum": AggregateFunction.MIN,
    "max": AggregateFunction.MAX, "maximum": AggregateFunction.MAX,
    "count": AggregateFunction.COUNT, "n": AggregateFunction.COUNT, "rows": AggregateFunction.COUNT,
    "count_distinct": AggregateFunction.COUNT_DISTINCT, "distinct": AggregateFunction.COUNT_DISTINCT,
    "nunique": AggregateFunction.COUNT_DISTINCT, "distinct_count": AggregateFunction.COUNT_DISTINCT,
}
_STEP_TYPE_ALIASES = {
    "select": "select", "project": "select", "columns": "select", "keep": "select",
    "filter": "filter", "where": "filter",
    "aggregate": "aggregate", "group": "aggregate", "groupby": "aggregate", "group_by": "aggregate", "summarize": "aggregate",
    "sort": "sort", "order": "sort", "order_by": "sort", "orderby": "sort",
    "limit": "limit", "head": "limit", "top": "limit",
    "rename": "rename",
    "derive": "derive", "compute": "derive", "mutate": "derive", "add_column": "derive",
    "join": "join", "merge": "join",
}
_IMPLICIT_ORDER = ("join", "filter", "derive", "select", "aggregate", "sort", "limit", "rename")
_IMPLICIT_KEYS = {
    "join": {"join"},
    "filter": {"filter", "where"},
    "derive": {"derive", "compute"},
    "select": {"select", "columns"},
    "aggregate": {"group_by", "groupby", "measures", "metric", "metrics", "aggregate"},
    "sort": {"sort", "order_by", "sort_by"},
    "limit": {"limit", "top", "head"},
    "rename": {"rename"},
}


class TransformResolver:
    def __init__(self, datasets: DatasetResolver, fields: FieldResolver | None = None) -> None:
        self.datasets = datasets
        self.fields = fields or FieldResolver()
        self.expressions = ExpressionResolver(self.fields)

    # -- public ----------------------------------------------------------
    def resolve(
        self,
        *,
        source: Any,
        transform: Any,
        output_name: str | None,
        description: str | None,
        preview_limit: int,
        context: dict[str, Any] | None,
        notes: list[ResolutionNote],
    ) -> tuple[TransformIR, list[Dataset]]:
        dataset = self.datasets.resolve(source, field="source", context=context, notes=notes)
        scope = Scope(fields=[ScopeField.from_column(c) for c in dataset.columns])
        input_schema = scope.refs()
        used = [dataset]

        steps: list[Step] = []
        for index, loose_step in enumerate(self._normalize_steps(transform)):
            step, scope = self._resolve_step(loose_step, scope, index, context, notes, used)
            steps.append(step)

        if output_name is not None:
            name = slugify(output_name)
            if name != output_name:
                notes.append(ResolutionNote("output_name", output_name, name, "normalized to snake_case"))
            output = OutputSpec(mode=OutputMode.MATERIALIZED, name=name, description=description or "", preview_limit=preview_limit)
        else:
            output = OutputSpec(mode=OutputMode.PREVIEW, preview_limit=preview_limit)

        ir = TransformIR(
            source=DatasetRef(dataset_id=dataset.id, version=dataset.version, name=dataset.name),
            steps=steps,
            input_schema=input_schema,
            output_schema=scope.refs(),
            output=output,
        )
        return ir, used

    # -- step normalization ---------------------------------------------
    def _normalize_steps(self, transform: Any) -> list[dict[str, Any]]:
        if transform is None:
            return []
        if isinstance(transform, str):
            raise InvalidTransformError(
                "transform must be an object (or list of steps), not a string.",
                field="transform",
                hint='Example: {"type": "aggregate", "group_by": ["region"], "measures": [{"function": "sum", "field": "amount"}]}',
            )
        if isinstance(transform, list):
            return [self._normalize_one(s) for s in transform]
        if not isinstance(transform, dict):
            raise InvalidTransformError("transform must be an object or a list of step objects.", field="transform")
        if "steps" in transform:
            reject_unknown_keys(transform, {"steps"}, "transform")
            return [self._normalize_one(s) for s in transform["steps"]]
        if "type" in transform or "op" in transform or "operation" in transform:
            return [self._normalize_one(transform)]
        # implicit compound form: {"filter": ..., "group_by": [...], "metric": "revenue", "sort": ..., "limit": 5}
        keys = set(transform)
        all_known = set().union(*_IMPLICIT_KEYS.values())
        reject_unknown_keys(transform, all_known | {"type"}, "transform")
        steps: list[dict[str, Any]] = []
        for step_type in _IMPLICIT_ORDER:
            present = keys & _IMPLICIT_KEYS[step_type]
            if not present:
                continue
            if step_type == "aggregate":
                payload = {k: transform[k] for k in present}
            else:
                if len(present) > 1:
                    raise InvalidTransformError(
                        f"Use only one of {sorted(present)} for the {step_type} step.", field="transform"
                    )
                (key,) = tuple(present)
                payload = {key: transform[key]}
            steps.append({"type": step_type, **payload})
        return steps

    @staticmethod
    def _normalize_one(step: Any) -> dict[str, Any]:
        if not isinstance(step, dict):
            raise InvalidTransformError(f"Each step must be an object, got {step!r}.", field="transform")
        raw = pick(step, "type", "op", "operation")
        if raw is None:
            raise InvalidTransformError(
                "Each step needs a 'type'.", field="transform",
                details={"allowed_types": sorted(set(_STEP_TYPE_ALIASES.values()))},
            )
        step_type = _STEP_TYPE_ALIASES.get(str(raw).lower())
        if step_type is None:
            raise InvalidTransformError(
                f"Unknown step type {raw!r}.", field="transform",
                details={"allowed_types": sorted(set(_STEP_TYPE_ALIASES.values()))},
            )
        body = {k: v for k, v in step.items() if k not in ("type", "op", "operation")}
        return {"type": step_type, **body}

    # -- step resolution -------------------------------------------------
    def _resolve_step(
        self,
        loose: dict[str, Any],
        scope: Scope,
        index: int,
        context: dict[str, Any] | None,
        notes: list[ResolutionNote],
        used: list[Dataset],
    ) -> tuple[Step, Scope]:
        step_type = loose["type"]
        where = f"transform.steps[{index}] ({step_type})"
        handler = getattr(self, f"_step_{step_type}")
        return handler(loose, scope, where, context, notes, used)

    def _step_select(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, {"type", "select", "fields", "columns"}, where)
        raw = pick(loose, "select", "fields", "columns")
        names = [raw] if isinstance(raw, str) else raw
        if not isinstance(names, list) or not names:
            raise InvalidTransformError("select needs a non-empty list of fields.", field=where)
        fields = [self.fields.resolve(n, scope, field=where, notes=notes) for n in names]
        self._check_unique([f.name for f in fields], where)
        new_scope = scope.with_fields(fields)
        return SelectStep(fields=[f.ref() for f in fields], output_schema=new_scope.refs()), new_scope

    def _step_filter(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, {"type", "filter", "where", "predicate", "condition", "expression", "expr"}, where)
        raw = pick(loose, "filter", "where", "predicate", "condition", "expression", "expr")
        if raw is None:
            raise InvalidTransformError("filter needs a predicate.", field=where)
        predicate = self.expressions.resolve(raw, scope, field=where, notes=notes)
        if predicate.logical_type != LogicalType.BOOLEAN:
            raise InvalidTransformError(
                f"filter predicate must be boolean, got {predicate.logical_type}.", field=where
            )
        return FilterStep(predicate=predicate, output_schema=scope.refs()), scope

    def _step_aggregate(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, {"type", "group_by", "groupby", "by", "measures", "metrics", "metric", "aggregate"}, where)
        raw_group = pick(loose, "group_by", "groupby", "by", default=[])
        group_names = [raw_group] if isinstance(raw_group, str) else list(raw_group or [])
        group_fields = [self.fields.resolve(n, scope, field=f"{where}.group_by", notes=notes) for n in group_names]
        self._check_unique([f.name for f in group_fields], f"{where}.group_by")

        raw_measures = pick(loose, "measures", "metrics", "metric", "aggregate", default=[])
        if isinstance(raw_measures, (str, dict)):
            raw_measures = [raw_measures]
        if not raw_measures:
            raise InvalidTransformError(
                "aggregate needs at least one measure.",
                field=where,
                hint='Example: "measures": [{"function": "sum", "field": "amount", "alias": "revenue"}]',
            )
        measures = [self._measure(m, scope, f"{where}.measures[{i}]", notes) for i, m in enumerate(raw_measures)]
        self._check_unique([f.name for f in group_fields] + [m.alias for m in measures], where)

        out_fields = [ScopeField(name=f.name, logical_type=f.logical_type, column_id=f.column_id, aliases=f.aliases, semantic_role=f.semantic_role) for f in group_fields]
        out_fields += [ScopeField(name=m.alias, logical_type=m.logical_type, semantic_role=SemanticRole.MEASURE) for m in measures]
        new_scope = scope.with_fields(out_fields)
        return AggregateStep(group_by=[f.ref() for f in group_fields], measures=measures, output_schema=new_scope.refs()), new_scope

    def _measure(self, loose: Any, scope: Scope, where: str, notes: list[ResolutionNote]) -> Measure:
        if isinstance(loose, str):
            # "revenue" → sum(resolved field) aliased "revenue"; "count" → count(*)
            if loose.strip().lower() in ("count", "rows", "n", "count(*)"):
                return Measure(function=AggregateFunction.COUNT, field=None, alias="count", logical_type=LogicalType.INTEGER)
            field = self.fields.resolve_measure_field(loose, scope, field=where, notes=notes)
            alias = slugify(loose)
            return Measure(function=AggregateFunction.SUM, field=field.ref(), alias=alias,
                           logical_type=aggregate_result_type(AggregateFunction.SUM, field.logical_type))
        if not isinstance(loose, dict):
            raise InvalidTransformError(f"Measure must be a string or object, got {loose!r}.", field=where)
        loose = dict(loose)
        # {"sum": "amount", "as": "revenue"} shorthand
        for key in list(loose):
            if key.lower() in _AGG_ALIASES and "function" not in loose and "fn" not in loose and "agg" not in loose:
                loose["function"] = key
                loose["field"] = loose.pop(key)
        reject_unknown_keys(loose, {"function", "fn", "agg", "field", "column", "of", "alias", "as", "name"}, where)
        raw_fn = pick(loose, "function", "fn", "agg")
        if raw_fn is None:
            raise InvalidTransformError("Measure needs a 'function' (sum, avg, min, max, count, count_distinct).", field=where)
        function = _AGG_ALIASES.get(str(raw_fn).lower())
        if function is None:
            raise InvalidTransformError(
                f"Unknown aggregate function {raw_fn!r}.", field=where,
                details={"allowed_functions": [str(f) for f in AggregateFunction]},
            )
        raw_field = pick(loose, "field", "column", "of")
        alias = pick(loose, "alias", "as", "name")
        if raw_field in (None, "*", ""):
            if function != AggregateFunction.COUNT:
                raise InvalidTransformError(f"Measure {function} needs a field.", field=where)
            return Measure(function=function, field=None, alias=slugify(alias or "count"), logical_type=LogicalType.INTEGER)
        field = self.fields.resolve_measure_field(raw_field, scope, field=where, notes=notes)
        result_type = aggregate_result_type(function, field.logical_type)
        return Measure(function=function, field=field.ref(), alias=slugify(alias or f"{function}_{field.name}"), logical_type=result_type)

    def _step_sort(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, {"type", "sort", "by", "keys", "order_by", "sort_by", "direction", "order",
                                    "field", "fields", "column", "columns"}, where)
        raw = pick(loose, "sort", "by", "keys", "order_by", "sort_by", "field", "fields", "column", "columns")
        if "order" in loose:
            # "order" is a direction when it is asc/desc, otherwise the list of keys.
            if isinstance(loose["order"], str) and loose["order"].strip().lower() in ("asc", "desc", "ascending", "descending"):
                if "direction" not in loose:
                    loose = {**loose, "direction": loose["order"]}
            elif raw is None:
                raw = loose["order"]
        items = [raw] if isinstance(raw, (str, dict)) else list(raw or [])
        if not items:
            raise InvalidTransformError("sort needs at least one key.", field=where)
        default_direction = str(loose.get("direction", "asc")).lower()
        keys: list[SortKey] = []
        for item in items:
            direction = default_direction
            if isinstance(item, str):
                name = item.strip()
                if name.startswith("-"):
                    name, direction = name[1:], "desc"
                elif name.startswith("+"):
                    name = name[1:]
                elif name.lower().endswith(" desc"):
                    name, direction = name[:-5].strip(), "desc"
                elif name.lower().endswith(" asc"):
                    name = name[:-4].strip()
            elif isinstance(item, dict):
                reject_unknown_keys(item, {"field", "column", "name", "direction", "order", "desc"}, where)
                name = pick(item, "field", "column", "name")
                if "desc" in item:
                    direction = "desc" if item["desc"] else "asc"
                else:
                    direction = str(pick(item, "direction", "order", default=direction)).lower()
            else:
                raise InvalidTransformError(f"Invalid sort key {item!r}.", field=where)
            direction = {"ascending": "asc", "descending": "desc"}.get(direction, direction)
            if direction not in ("asc", "desc"):
                raise InvalidTransformError(f"Sort direction must be asc or desc, got {direction!r}.", field=where)
            field = self.fields.resolve(name, scope, field=where, notes=notes)
            keys.append(SortKey(field=field.ref(), direction=direction))
        return SortStep(keys=keys, output_schema=scope.refs()), scope

    def _step_limit(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, {"type", "limit", "n", "count", "offset", "top", "head"}, where)
        raw = pick(loose, "limit", "n", "count", "top", "head")
        if isinstance(raw, dict):
            reject_unknown_keys(raw, {"limit", "n", "count", "offset"}, where)
            offset = raw.get("offset", loose.get("offset", 0))
            raw = pick(raw, "limit", "n", "count")
        else:
            offset = loose.get("offset", 0)
        try:
            limit = int(raw)
            offset = int(offset or 0)
        except (TypeError, ValueError) as exc:
            raise InvalidTransformError(f"limit must be an integer, got {raw!r}.", field=where) from exc
        if limit < 0 or offset < 0:
            raise InvalidTransformError("limit and offset must be non-negative.", field=where)
        return LimitStep(limit=limit, offset=offset, output_schema=scope.refs()), scope

    def _step_rename(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, {"type", "rename", "mappings", "from", "to"}, where)
        raw = pick(loose, "rename", "mappings")
        pairs: list[tuple[Any, Any]] = []
        if raw is None and "from" in loose:
            pairs.append((loose["from"], loose["to"]))
        elif isinstance(raw, dict):
            pairs.extend(raw.items())
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    raise InvalidTransformError(f"Invalid rename mapping {item!r}.", field=where)
                reject_unknown_keys(item, {"from", "to", "field", "column", "name", "new_name", "alias"}, where)
                pairs.append((pick(item, "from", "field", "column", "name"), pick(item, "to", "new_name", "alias")))
        if not pairs:
            raise InvalidTransformError('rename needs mappings such as {"rename": {"old": "new"}}.', field=where)
        mappings: list[RenameMapping] = []
        renamed: dict[str, str] = {}
        for old, new in pairs:
            if not isinstance(new, str) or not new.strip():
                raise InvalidTransformError(f"Invalid new name {new!r}.", field=where)
            field = self.fields.resolve(old, scope, field=where, notes=notes)
            new_name = slugify(new)
            if new_name != new:
                notes.append(ResolutionNote(where, new, new_name, "normalized to snake_case"))
            mappings.append(RenameMapping(field=field.ref(), to=new_name))
            renamed[field.name] = new_name
        new_fields = [
            ScopeField(name=renamed.get(f.name, f.name), logical_type=f.logical_type, column_id=f.column_id,
                       aliases=f.aliases, semantic_role=f.semantic_role)
            for f in scope.fields
        ]
        self._check_unique([f.name for f in new_fields], where)
        new_scope = scope.with_fields(new_fields)
        return RenameStep(mappings=mappings, output_schema=new_scope.refs()), new_scope

    def _step_derive(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, {"type", "derive", "compute", "name", "as", "alias", "expression", "expr", "formula"}, where)
        raw = pick(loose, "derive", "compute")
        name = pick(loose, "name", "as", "alias")
        expression = pick(loose, "expression", "expr", "formula")
        if isinstance(raw, dict) and name is None and expression is None:
            if "name" in raw or "expression" in raw or "expr" in raw:
                reject_unknown_keys(raw, {"name", "as", "alias", "expression", "expr", "formula"}, where)
                name = pick(raw, "name", "as", "alias")
                expression = pick(raw, "expression", "expr", "formula")
            elif len(raw) == 1:
                (name, expression), = raw.items()
            else:
                raise InvalidTransformError('derive needs exactly one {"name": "expression"} pair per step.', field=where)
        elif raw is not None and expression is None:
            expression = raw
        if not isinstance(name, str) or not name.strip() or expression is None:
            raise InvalidTransformError(
                'derive needs a name and an expression, e.g. {"type": "derive", "name": "total", "expression": "quantity * unit_price"}.',
                field=where,
            )
        expr = self.expressions.resolve(expression, scope, field=where, notes=notes)
        new_name = slugify(name)
        if new_name != name:
            notes.append(ResolutionNote(where, name, new_name, "normalized to snake_case"))
        self._check_unique(scope.names() + [new_name], where)
        new_scope = scope.with_fields(
            scope.fields + [ScopeField(name=new_name, logical_type=expr.logical_type,
                                       semantic_role=SemanticRole.MEASURE if expr.logical_type.is_numeric else SemanticRole.UNKNOWN)]
        )
        return DeriveStep(name=new_name, expression=expr, logical_type=expr.logical_type, output_schema=new_scope.refs()), new_scope

    def _step_join(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, {"type", "join", "right", "with", "dataset", "on", "how", "kind", "columns", "select"}, where)
        raw_right = pick(loose, "right", "with", "dataset", "join")
        if isinstance(raw_right, dict) and ("on" in raw_right or "how" in raw_right):
            loose = {**raw_right, **{k: v for k, v in loose.items() if k not in ("join", "right", "with", "dataset")}}
            raw_right = pick(raw_right, "right", "with", "dataset")
        right = self.datasets.resolve(raw_right, field=f"{where}.right", context=context, notes=notes)
        used.append(right)
        right_scope = Scope(fields=[ScopeField.from_column(c) for c in right.columns])
        how = str(pick(loose, "how", "kind", default="inner")).lower()
        if how not in ("inner", "left", "right", "full"):
            raise InvalidTransformError(f"join 'how' must be inner, left, right or full; got {how!r}.", field=where)

        raw_on = loose.get("on")
        if raw_on is None:
            raise InvalidTransformError('join needs "on": either a shared field name, a list, or {"left_field": "right_field"}.', field=where)
        pairs: list[tuple[Any, Any]] = []
        if isinstance(raw_on, str):
            pairs.append((raw_on, raw_on))
        elif isinstance(raw_on, dict):
            if "left" in raw_on and "right" in raw_on:
                pairs.append((raw_on["left"], raw_on["right"]))
            else:
                pairs.extend(raw_on.items())
        elif isinstance(raw_on, list):
            for item in raw_on:
                if isinstance(item, str):
                    pairs.append((item, item))
                elif isinstance(item, dict):
                    reject_unknown_keys(item, {"left", "right"}, where)
                    pairs.append((item.get("left"), item.get("right")))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    pairs.append((item[0], item[1]))
                else:
                    raise InvalidTransformError(f"Invalid join condition {item!r}.", field=where)
        conditions: list[JoinCondition] = []
        for left_name, right_name in pairs:
            left_field = self.fields.resolve(left_name, scope, field=f"{where}.on.left", notes=notes)
            right_field = self.fields.resolve(right_name, right_scope, field=f"{where}.on.right", notes=notes)
            from ..ir.typing import comparable

            if not comparable(left_field.logical_type, right_field.logical_type):
                raise InvalidTransformError(
                    f"Cannot join {left_field.name} ({left_field.logical_type}) with {right_field.name} ({right_field.logical_type}).",
                    field=where,
                )
            conditions.append(JoinCondition(left=left_field.ref(), right=right_field.ref()))

        key_names = {c.right.name for c in conditions}
        raw_columns = pick(loose, "columns", "select")
        if raw_columns is not None:
            wanted = [self.fields.resolve(n, right_scope, field=f"{where}.columns", notes=notes) for n in ([raw_columns] if isinstance(raw_columns, str) else raw_columns)]
        else:
            wanted = [f for f in right_scope.fields if f.name not in key_names]
        existing = set(scope.names())
        outputs: list[JoinOutput] = []
        new_fields = list(scope.fields)
        for f in wanted:
            out_name = f.name if f.name not in existing else f"{f.name}_{right.name}"
            if out_name in existing:
                raise InvalidTransformError(f"Join output name {out_name!r} collides with an existing field.", field=where)
            existing.add(out_name)
            outputs.append(JoinOutput(field=f.ref(), output_name=out_name))
            new_fields.append(ScopeField(name=out_name, logical_type=f.logical_type, column_id=f.column_id, aliases=f.aliases, semantic_role=f.semantic_role))
        new_scope = scope.with_fields(new_fields)
        step = JoinStep(
            right=DatasetRef(dataset_id=right.id, version=right.version, name=right.name),
            how=how,
            on=conditions,
            right_fields=outputs,
            output_schema=new_scope.refs(),
        )
        return step, new_scope

    @staticmethod
    def _check_unique(names: list[str], where: str) -> None:
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise InvalidTransformError(f"Duplicate output field {name!r}.", field=where)
            seen.add(name)
