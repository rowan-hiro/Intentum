"""Resolve a loose transform intent into a canonical ``TransformIR``.

The resolver walks the requested steps in order, maintaining the relation
schema ("scope") that is current after each step, so that later steps can
reference fields produced by earlier ones (for example sorting by an
aggregate alias). Every lenient decision is recorded as a ``ResolutionNote``.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import BackendError, InvalidIntentError, InvalidTransformError, NotFoundError
from ..ir import (
    AggregateFunction,
    AggregateStep,
    DatasetRef,
    DeriveStep,
    FieldRef,
    FilterStep,
    JoinCondition,
    JoinOutput,
    JoinStep,
    LimitStep,
    Measure,
    OutputMode,
    OutputSpec,
    QueryInput,
    RawQueryStep,
    RenameMapping,
    RenameStep,
    SemiJoinStep,
    SelectStep,
    SortKey,
    SortStep,
    Step,
    TransformIR,
)
from ..ir.raw_query import DEFAULT_PLACEHOLDER, QueryBinding, QueryEngine, analyze_query, check_placeholder, refusal
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
# A text measure that is really an aggregate call: "max(amount) as peak", "sum(x)".
_MEASURE_EXPRESSION_RE = re.compile(r"\w+\s*\(|\s+as\s+", re.IGNORECASE)


def _looks_like_expression_text(text: str) -> bool:
    """Whether a derive text computes something (operators, a call, a literal) rather than naming one thing."""
    return bool(re.search(r"[()+\-*/%<>=']|\s+as\s+|\bis\b|\band\b|\bor\b|\bnot\b|\bin\b", text, re.IGNORECASE))
_STEP_TYPE_ALIASES = {
    "select": "select", "project": "select", "columns": "select", "keep": "select",
    "filter": "filter", "where": "filter",
    "aggregate": "aggregate", "group": "aggregate", "groupby": "aggregate", "group_by": "aggregate", "summarize": "aggregate",
    "sort": "sort", "order": "sort", "order_by": "sort", "orderby": "sort",
    "limit": "limit", "head": "limit", "top": "limit",
    "rename": "rename",
    "derive": "derive", "compute": "derive", "mutate": "derive", "add_column": "derive",
    "join": "join", "merge": "join",
    "semi_join": "semi_join", "semijoin": "semi_join", "where_exists": "semi_join",
    "raw_query": "raw_query", "sql": "raw_query",
}
# The keys each step handler accepts, besides "type". A single-key untyped step
# whose value is an object made only of these keys carries the step body nested
# under its own name: {"aggregate": {"group_by": [...], "measures": [...]}}.
_STEP_BODY_KEYS: dict[str, set[str]] = {
    "select": {"select", "fields", "columns"},
    "filter": {"filter", "where", "predicate", "condition", "expression", "expr"},
    "aggregate": {"group_by", "groupby", "by", "measures", "metrics", "metric", "aggregate"},
    "sort": {"sort", "by", "keys", "order_by", "sort_by", "direction", "order", "field", "fields", "column", "columns"},
    "limit": {"limit", "n", "count", "offset", "top", "head"},
    "rename": {"rename", "mappings", "from", "to"},
    "derive": {"derive", "compute", "name", "as", "alias", "expression", "expr", "formula"},
    "join": {"join", "right", "with", "dataset", "on", "how", "kind", "columns", "select"},
    "semi_join": {"semi_join", "semijoin", "where_exists", "right", "with", "dataset", "on"},
    "raw_query": {"raw_query", "sql", "inputs"},
}
_AGGREGATE_BODY_KEYS = {"group_by", "groupby", "by", "measures", "metrics", "metric"}
# A raw_query reads datasets, so in a compact object it comes first and the other keys shape its result.
_IMPLICIT_ORDER = ("raw_query", "join", "semi_join", "filter", "derive", "select", "aggregate", "sort", "limit", "rename")
_IMPLICIT_KEYS = {
    "raw_query": {"raw_query", "sql", "inputs"},
    "join": {"join"},
    "semi_join": {"semi_join", "semijoin", "where_exists"},
    "filter": {"filter", "where"},
    "derive": {"derive", "compute"},
    "select": {"select", "columns"},
    "aggregate": {"group_by", "groupby", "measures", "metric", "metrics", "aggregate"},
    "sort": {"sort", "order_by", "sort_by"},
    "limit": {"limit", "top", "head"},
    "rename": {"rename"},
}


_ALIAS_RE = re.compile(r"""^(?P<head>.+?)\s+as\s+(?P<alias>"[^"]+"|'[^']+'|[^\s"']+)\s*$""", re.IGNORECASE)


def _looks_like_expression(text: str) -> bool:
    return any(ch in text for ch in "()+-*/%<>=|'") and not text.strip().startswith('"')


def split_alias(text: str) -> tuple[str, str | None]:
    """Split a trailing SQL-style ``... as name`` off a field reference or expression.

    Returns ``(text, None)`` when there is no alias. The caller decides whether
    the split is real: a field may legitimately be called ``x as y``.
    """
    match = _ALIAS_RE.match(text.strip())
    if match is None:
        return text, None
    alias = match.group("alias")
    if alias[:1] in ('"', "'"):
        alias = alias[1:-1]
    return match.group("head").strip(), alias


class TransformResolver:
    def __init__(self, datasets: DatasetResolver, fields: FieldResolver | None = None,
                 queries: QueryEngine | None = None) -> None:
        self.datasets = datasets
        self.fields = fields or FieldResolver()
        self.expressions = ExpressionResolver(self.fields)
        # Parses and describes raw_query statements; None where the engine offers no sandbox.
        self.queries = queries

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
            produced, scope = self._resolve_step(loose_step, scope, index, context, notes, used)
            steps.extend(produced)

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
            return [s for item in transform for s in self._normalize_one(item)]
        if not isinstance(transform, dict):
            raise InvalidTransformError("transform must be an object or a list of step objects.", field="transform")
        if "steps" in transform:
            reject_unknown_keys(transform, {"steps"}, "transform")
            return [s for item in transform["steps"] for s in self._normalize_one(item)]
        return self._normalize_one(transform)

    def _normalize_one(self, step: Any) -> list[dict[str, Any]]:
        """Turn one loose step object into one or more typed step dicts.

        A step may name its type ("type"/"op"/"operation"); a step that does not
        is read by its keys, so the bare ``{"filter": "..."}`` is a step too, and
        a compound object such as ``{"filter": ..., "group_by": [...], "limit": 5}``
        becomes several.
        """
        if not isinstance(step, dict):
            raise InvalidTransformError(f"Each step must be an object, got {step!r}.", field="transform")
        raw = pick(step, "type", "op", "operation")
        if raw is None:
            if len(step) == 1:
                (key, value), = step.items()
                step_type = _STEP_TYPE_ALIASES.get(str(key).lower())
                if step_type and isinstance(value, dict) and value and set(value) <= _STEP_BODY_KEYS[step_type]:
                    return [{"type": step_type, **value}]
            return self._implicit_steps(step)
        step_type = _STEP_TYPE_ALIASES.get(str(raw).lower())
        if step_type is None:
            raise InvalidTransformError(
                f"Unknown step type {raw!r}.", field="transform",
                details={"allowed_types": sorted(set(_STEP_TYPE_ALIASES.values()))},
            )
        body = {k: v for k, v in step.items() if k not in ("type", "op", "operation")}
        return [{"type": step_type, **body}]

    @classmethod
    def _implicit_steps(cls, transform: dict[str, Any]) -> list[dict[str, Any]]:
        """Read an untyped step object by its keys, in the canonical step order."""
        keys = set(transform)
        all_known = set().union(*_IMPLICIT_KEYS.values())
        reject_unknown_keys(transform, all_known | {"type"}, "transform")
        steps: list[dict[str, Any]] = []
        order = list(_IMPLICIT_ORDER)
        order.remove("select")
        if keys & _IMPLICIT_KEYS["aggregate"] and not cls._select_uses_aggregate_output(transform):
            order.insert(order.index("derive") + 1, "select")  # narrows the aggregate's input
        elif cls._sort_needs_unselected_field(transform):
            order.insert(order.index("limit") + 1, "select")  # sort by a field the projection drops
        else:
            order.insert(order.index("aggregate") + 1, "select")
        for step_type in order:
            present = keys & _IMPLICIT_KEYS[step_type]
            if not present:
                continue
            if step_type in ("aggregate", "raw_query"):
                payload = {k: transform[k] for k in present}
            else:
                if len(present) > 1:
                    raise InvalidTransformError(
                        f"Use only one of {sorted(present)} for the {step_type} step.", field="transform"
                    )
                (key,) = tuple(present)
                payload = {key: transform[key]}
            steps.append({"type": step_type, **payload})
        if not steps:
            raise InvalidTransformError(
                "A step needs a 'type' or a key that names one.", field="transform",
                details={"allowed_types": sorted(set(_STEP_TYPE_ALIASES.values())),
                         "allowed_keys": sorted(all_known)},
            )
        return steps

    @classmethod
    def _sort_needs_unselected_field(cls, transform: dict[str, Any]) -> bool:
        """Whether a compound object's sort names a field its projection would drop.

        Sorting does not change columns, so projecting after the sort is the same
        result whenever the key is kept, and the only valid reading when it is not.
        A sort by a select alias keeps the projection first.
        """
        raw_sort = pick(transform, "sort", "order_by", "sort_by")
        raw_select = pick(transform, "select", "columns")
        if raw_sort is None or raw_select is None:
            return False
        selected: set[str] = set()
        for item in [raw_select] if isinstance(raw_select, (str, dict)) else list(raw_select or []):
            if isinstance(item, dict):
                name = pick(item, "as", "alias", "to") or pick(item, "field", "column", "name")
            else:
                head, alias = split_alias(str(item))
                name = alias or head
            if name is not None:
                selected.add(slugify(str(name)))
        for item in [raw_sort] if isinstance(raw_sort, (str, dict)) else list(raw_sort or []):
            if isinstance(item, dict):
                name = pick(item, "field", "column", "name")
            else:
                name = str(item).strip().lstrip("-+")
                for suffix in (" desc", " asc"):
                    if name.lower().endswith(suffix):
                        name = name[: -len(suffix)].strip()
            if name is not None and slugify(str(name)) not in selected:
                return True
        return False

    @staticmethod
    def _select_uses_aggregate_output(transform: dict[str, Any]) -> bool:
        """Whether a compound object's projection needs an aggregate alias.

        Compact objects historically project before aggregating, which is
        still useful when ``select`` narrows the input. When a selected field
        is created by a measure in the same object, however, the projection
        can only be meaningful after the aggregate.
        """
        raw_select = pick(transform, "select", "columns")
        if raw_select is None or not (set(transform) & _IMPLICIT_KEYS["aggregate"]):
            return False
        selected = [raw_select] if isinstance(raw_select, (str, dict)) else list(raw_select or [])
        selected_names: set[str] = set()
        for item in selected:
            if isinstance(item, dict):
                name = pick(item, "field", "column", "name")
            else:
                name, _ = split_alias(str(item))
            if name is not None:
                selected_names.add(slugify(str(name)))

        body = transform.get("aggregate")
        if not isinstance(body, dict) or not set(body) & _AGGREGATE_BODY_KEYS:
            body = transform
        raw_measures = pick(body, "measures", "metrics", "metric", "aggregate", default=[])
        measures = [raw_measures] if isinstance(raw_measures, (str, dict)) else list(raw_measures or [])
        aliases: set[str] = set()
        for measure in measures:
            if isinstance(measure, str):
                aliases.add("count" if measure.strip().lower() in ("count", "rows", "n", "count(*)") else slugify(measure))
                continue
            if not isinstance(measure, dict):
                continue
            alias = pick(measure, "alias", "as", "name")
            raw_fn = pick(measure, "function", "fn", "agg")
            raw_field = pick(measure, "field", "column", "of")
            if raw_fn is None:
                for key, value in measure.items():
                    if str(key).lower() in _AGG_ALIASES:
                        raw_fn, raw_field = key, value
                        break
            function = _AGG_ALIASES.get(str(raw_fn).lower()) if raw_fn is not None else None
            if alias is not None:
                aliases.add(slugify(str(alias)))
            elif function == AggregateFunction.COUNT and raw_field in (None, "*", ""):
                aliases.add("count")
            elif function is not None and raw_field not in (None, "*", ""):
                aliases.add(slugify(f"{function}_{raw_field}"))
        return bool(selected_names & aliases)

    # -- step resolution -------------------------------------------------
    def _resolve_step(
        self,
        loose: dict[str, Any],
        scope: Scope,
        index: int,
        context: dict[str, Any] | None,
        notes: list[ResolutionNote],
        used: list[Dataset],
    ) -> tuple[list[Step], Scope]:
        step_type = loose["type"]
        where = f"transform.steps[{index}] ({step_type})"
        if step_type == "raw_query" and index > 0:
            raise refusal(
                "raw_query must be the first step: its placeholders bind datasets, and input is the transform's "
                "source, not the result of the steps before it. Semantic steps may follow it.",
                where, "not_first", index=index,
            )
        handler = getattr(self, f"_step_{step_type}")
        produced, scope = handler(loose, scope, where, context, notes, used)
        return (produced if isinstance(produced, list) else [produced]), scope

    def _step_select(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, _STEP_BODY_KEYS["select"] | {"type"}, where)
        raw = pick(loose, "select", "fields", "columns")
        items = [raw] if isinstance(raw, (str, dict)) else raw
        if not isinstance(items, list) or not items:
            raise InvalidTransformError("select needs a non-empty list of fields.", field=where)
        fields: list[ScopeField] = []
        aliases: list[str | None] = []
        for item in items:
            if isinstance(item, dict):
                reject_unknown_keys(item, {"field", "column", "name", "as", "alias", "to"}, where)
                reference = pick(item, "field", "column", "name")
                alias = pick(item, "as", "alias", "to")
                field = self.fields.resolve(reference, scope, field=where, notes=notes)
            else:
                field, alias = self._field_with_alias(str(item), scope, where, notes)
            fields.append(field)
            aliases.append(str(alias) if alias is not None else None)
        self._check_unique([f.name for f in fields], where, written=items, notes=notes, available=scope.names())
        selected = scope.with_fields(fields)
        steps: list[Step] = [SelectStep(fields=[f.ref() for f in fields], output_schema=selected.refs())]
        if all(a is None for a in aliases):
            return steps, selected
        # "amount as revenue" is a projection plus a rename; both stay visible in the IR.
        mappings: list[RenameMapping] = []
        renamed_fields: list[ScopeField] = []
        for field, alias in zip(fields, aliases):
            if alias is None:
                renamed_fields.append(field)
                continue
            new_name = slugify(alias)
            if new_name != alias:
                notes.append(ResolutionNote(where, alias, new_name, "normalized to snake_case"))
            if new_name != field.name:
                mappings.append(RenameMapping(field=field.ref(), to=new_name))
            renamed_fields.append(ScopeField(name=new_name, logical_type=field.logical_type, column_id=field.column_id,
                                             aliases=field.aliases, semantic_role=field.semantic_role))
        if not mappings:
            return steps, selected
        self._check_unique([f.name for f in renamed_fields], where)
        final = selected.with_fields(renamed_fields)
        steps.append(RenameStep(mappings=mappings, output_schema=final.refs()))
        return steps, final

    def _field_with_alias(self, text: str, scope: Scope, where: str, notes: list[ResolutionNote]):
        """Resolve a field reference that may carry a trailing ``as alias``.

        The split is only honoured when the part before ``as`` resolves; a field
        genuinely named ``x as y`` still wins.
        """
        head, alias = split_alias(text)
        if alias is None:
            return self.fields.resolve(text, scope, field=where, notes=notes), None
        trial: list[ResolutionNote] = []
        try:
            field = self.fields.resolve(head, scope, field=where, notes=trial)
        except BackendError:
            return self.fields.resolve(text, scope, field=where, notes=notes), None
        notes.extend(trial)
        return field, alias

    def _step_filter(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, _STEP_BODY_KEYS["filter"] | {"type"}, where)
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
        if isinstance(loose.get("aggregate"), dict) and set(loose["aggregate"]) & _AGGREGATE_BODY_KEYS:
            # {"aggregate": {"group_by": ..., "measures": ...}}: the body nested under its own key
            loose = {**loose["aggregate"], **{k: v for k, v in loose.items() if k != "aggregate"}}
        reject_unknown_keys(loose, _STEP_BODY_KEYS["aggregate"] | {"type"}, where)
        raw_group = pick(loose, "group_by", "groupby", "by", default=[])
        group_items = [raw_group] if isinstance(raw_group, (str, dict)) else list(raw_group or [])
        pre_steps: list[Step] = []
        group_fields: list[ScopeField] = []
        for item in group_items:
            field, derived, scope = self._group_key(item, scope, where, notes)
            if derived is not None:
                pre_steps.append(derived)
            group_fields.append(field)
        self._check_unique([f.name for f in group_fields], f"{where}.group_by")

        raw_measures = pick(loose, "measures", "metrics", "metric", "aggregate", default=[])
        if isinstance(raw_measures, (str, dict)):
            raw_measures = [raw_measures]
        if not raw_measures and not group_fields:
            raise InvalidTransformError(
                "aggregate needs at least one group_by field or measure.",
                field=where,
                hint='Example: "group_by": ["region"], or "measures": [{"function": "sum", "field": "amount", "alias": "revenue"}]',
            )
        measures = [self._measure(m, scope, f"{where}.measures[{i}]", notes) for i, m in enumerate(raw_measures)]
        self._check_unique([f.name for f in group_fields] + [m.alias for m in measures], where,
                           written=list(group_items) + list(raw_measures), notes=notes, available=scope.names())

        out_fields = [ScopeField(name=f.name, logical_type=f.logical_type, column_id=f.column_id, aliases=f.aliases, semantic_role=f.semantic_role) for f in group_fields]
        out_fields += [ScopeField(name=m.alias, logical_type=m.logical_type, semantic_role=SemanticRole.MEASURE) for m in measures]
        new_scope = scope.with_fields(out_fields)
        step = AggregateStep(group_by=[f.ref() for f in group_fields], measures=measures, output_schema=new_scope.refs())
        return pre_steps + [step], new_scope

    def _group_key(self, item: Any, scope: Scope, where: str, notes: list[ResolutionNote]):
        """A grouping key: a field, or an expression that is derived first and grouped by its alias.

        ``"strftime(t, '%Y-%m-%d') as day"`` or ``{"day": "strftime(t, '%Y-%m-%d')"}``
        become a derive step ahead of the aggregate; the aggregate then groups by
        ``day`` like any other field. An expression without a name is refused
        rather than named on the agent's behalf.
        """
        field_where = f"{where}.group_by"
        if isinstance(item, dict):
            try:
                name, expression = self._derive_pair(item, field_where)
            except InvalidTransformError as err:
                err.message = (f"Invalid group_by key {item!r}; a key is a field name, an \"expression as name\", "
                               "or a {\"name\": \"expression\"} object.")
                raise
        elif isinstance(item, str):
            head, alias = split_alias(item)
            if alias is None:
                try:
                    return self.fields.resolve(item, scope, field=field_where, notes=notes), None, scope
                except NotFoundError:
                    if not _looks_like_expression(item):
                        raise
                    raise InvalidTransformError(
                        f"group_by cannot use the expression {item!r} without a name.", field=field_where,
                        hint=f'Name it: "{item} as day", or derive it first and group by the new field.',
                    ) from None
            name, expression = alias, head
        else:
            raise InvalidTransformError(f"Invalid group_by key {item!r}.", field=field_where)
        derived, new_scope = self._derive_one(name, expression, scope, field_where, notes)
        notes.append(ResolutionNote(field_where, str(item), derived.name, "derived before grouping"))
        return new_scope.get(derived.name), derived, new_scope

    def _measure(self, loose: Any, scope: Scope, where: str, notes: list[ResolutionNote]) -> Measure:
        if isinstance(loose, str):
            # "revenue" → sum(resolved field) aliased "revenue"; "count" → count(*)
            if loose.strip().lower() in ("count", "rows", "n", "count(*)"):
                return Measure(function=AggregateFunction.COUNT, field=None, alias="count", logical_type=LogicalType.INTEGER)
            if _MEASURE_EXPRESSION_RE.search(loose):
                # An aggregate call written as text where a field name is expected: not a name to disambiguate.
                raise InvalidTransformError(
                    f"Measure {loose!r} is an expression; a text measure names a field to sum, and an aggregate call "
                    'is an object such as {"function": "max", "field": "amount", "alias": "max_amount"}.',
                    field=where,
                    details={"received": loose, "allowed_keys": ["function", "field", "alias"],
                             "allowed_functions": [str(f) for f in AggregateFunction]},
                )
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
                numeric = [f.name for f in scope.fields if f.logical_type.is_numeric]
                shown = ", ".join(numeric) if numeric else "none"
                raise InvalidTransformError(
                    f"Measure {function} needs a field; '*' counts rows and works only with count. "
                    f"Numeric fields here: {shown}.",
                    field=where,
                    details={"function": str(function), "received": loose, "numeric_fields": numeric,
                             "allowed_without_field": ["count"]},
                )
            return Measure(function=function, field=None, alias=slugify(alias or "count"), logical_type=LogicalType.INTEGER)
        field = self.fields.resolve_measure_field(raw_field, scope, field=where, notes=notes)
        result_type = aggregate_result_type(function, field.logical_type)
        return Measure(function=function, field=field.ref(), alias=slugify(alias or f"{function}_{field.name}"), logical_type=result_type)

    def _step_sort(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, _STEP_BODY_KEYS["sort"] | {"type"}, where)
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
        reject_unknown_keys(loose, _STEP_BODY_KEYS["limit"] | {"type"}, where)
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
        reject_unknown_keys(loose, _STEP_BODY_KEYS["rename"] | {"type"}, where)
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
        reject_unknown_keys(loose, _STEP_BODY_KEYS["derive"] | {"type"}, where)
        raw = pick(loose, "derive", "compute")
        name = pick(loose, "name", "as", "alias")
        expression = pick(loose, "expression", "expr", "formula")
        pairs: list[tuple[Any, Any]] = []
        if name is not None or expression is not None:
            pairs.append((name, expression if expression is not None else raw))
        elif isinstance(raw, list):
            pairs.extend(self._derive_pair(item, where) for item in raw)
        elif isinstance(raw, dict):
            if any(k in raw for k in ("name", "as", "alias", "expression", "expr", "formula")):
                pairs.append(self._derive_pair(raw, where))
            else:
                pairs.extend(raw.items())
        elif raw is not None:
            pairs.append((None, raw))
        if not pairs:
            raise InvalidTransformError(
                'derive needs a name and an expression, e.g. {"type": "derive", "name": "total", "expression": "quantity * unit_price"}.',
                field=where,
                details={"received": raw, "shape": {"name": "total", "expression": "quantity * unit_price"},
                         "available": scope.names()},
            )
        steps: list[Step] = []
        for derived_name, derived_expression in pairs:
            step, scope = self._derive_one(derived_name, derived_expression, scope, where, notes)
            steps.append(step)
        return steps, scope

    @staticmethod
    def _derive_pair(item: Any, where: str) -> tuple[Any, Any]:
        if isinstance(item, str):
            return None, item
        if isinstance(item, dict):
            if any(k in item for k in ("name", "as", "alias", "expression", "expr", "formula")):
                reject_unknown_keys(item, {"name", "as", "alias", "expression", "expr", "formula"}, where)
                return pick(item, "name", "as", "alias"), pick(item, "expression", "expr", "formula")
            if len(item) == 1:
                (pair,) = item.items()
                return pair
        raise InvalidTransformError(
            f"Invalid derive entry {item!r}; a derive entry is {{\"name\": \"total\", \"expression\": \"quantity * unit_price\"}} "
            "or {\"total\": \"quantity * unit_price\"}.",
            field=where,
            details={"received": item, "entry_keys": sorted(item) if isinstance(item, dict) else [],
                     "allowed_keys": ["name", "as", "alias", "expression", "expr", "formula"]},
        )

    def _derive_one(self, name: Any, expression: Any, scope: Scope, where: str, notes: list[ResolutionNote]):
        expr = None
        if isinstance(expression, str):
            head, alias = split_alias(expression)
            if alias is not None:
                # "quantity * unit_price as total": only a head that resolves makes it an alias.
                trial: list[ResolutionNote] = []
                try:
                    expr = self.expressions.resolve(head, scope, field=where, notes=trial)
                except BackendError:
                    expr = None
                else:
                    notes.extend(trial)
                    if name is None:
                        name = alias
                    elif slugify(str(name)) != slugify(alias):
                        raise InvalidTransformError(
                            f"derive names the result twice: {name!r} and {alias!r}.", field=where)
        if not isinstance(name, str) or not name.strip() or expression is None:
            # A bare name reaches here as the expression with no name: what was received is the name alone.
            received = {"name": name, "expression": expression}
            if name is None and isinstance(expression, str) and expression.strip() and not _looks_like_expression_text(expression):
                received = {"name": expression.strip(), "expression": None}
            raise InvalidTransformError(
                'derive needs a name and an expression, e.g. {"type": "derive", "name": "total", "expression": "quantity * unit_price"}.',
                field=where,
                details={"received": received, "shape": {"name": "total", "expression": "quantity * unit_price"},
                         "available": scope.names()},
            )
        if expr is None:
            expr = self.expressions.resolve(expression, scope, field=where, notes=notes)
        new_name = slugify(name)
        if new_name != name:
            notes.append(ResolutionNote(where, name, new_name, "normalized to snake_case"))
        self._check_unique(scope.names() + [new_name], where, written=scope.names() + [name], available=scope.names())
        new_scope = scope.with_fields(
            scope.fields + [ScopeField(name=new_name, logical_type=expr.logical_type,
                                       semantic_role=SemanticRole.MEASURE if expr.logical_type.is_numeric else SemanticRole.UNKNOWN)]
        )
        return DeriveStep(name=new_name, expression=expr, logical_type=expr.logical_type, output_schema=new_scope.refs()), new_scope

    def _step_join(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, _STEP_BODY_KEYS["join"] | {"type"}, where)
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
        for index, (left_name, right_name) in enumerate(pairs):
            left_field = self.fields.resolve(left_name, scope, field=f"{where}.on.left", notes=notes)
            right_field = self.fields.resolve(right_name, right_scope, field=f"{where}.on.right", notes=notes)
            from ..ir.typing import comparable

            if not comparable(left_field.logical_type, right_field.logical_type):
                raise InvalidTransformError(
                    f"Cannot join {left_field.name} ({left_field.logical_type}) with {right_field.name} ({right_field.logical_type}).",
                    field=where,
                    # What the advice needs to cast one key: both keys, their types, and the on as written.
                    details={"join_key_types": {
                        "left": left_field.name, "left_type": str(left_field.logical_type),
                        "right": right_field.name, "right_type": str(right_field.logical_type), "right_dataset": right.name,
                        "on": raw_on, "pairs": [list(p) for p in pairs], "failed": index,
                    }},
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

    def _step_semi_join(self, loose, scope, where, context, notes, used):
        reject_unknown_keys(loose, _STEP_BODY_KEYS["semi_join"] | {"type"}, where)
        body = dict(loose)
        body["type"] = "join"
        for key in ("semi_join", "semijoin", "where_exists"):
            if key in body:
                body["join"] = body.pop(key)
        body["columns"] = []
        join, joined_scope = self._step_join(body, scope, where, context, notes, used)
        if joined_scope.names() != scope.names():
            raise InvalidTransformError("semi_join cannot add fields from the right dataset.", field=where)
        step = SemiJoinStep(right=join.right, on=join.on, output_schema=scope.refs())
        return step, scope

    def _step_raw_query(self, loose, scope, where, context, notes, used):
        """Read-only SQL over placeholders: ``input`` is the source, each name under ``inputs`` a dataset (MADR 0002).

        ``{"raw_query": "SELECT ... FROM input"}``, or with more inputs
        ``{"raw_query": {"sql": "...", "inputs": {"customers": "customers"}}}``;
        ``inputs`` may also list names that are dataset references themselves.
        The output schema is DuckDB's DESCRIBE of the statement, mapped to the
        backend's types.
        """
        reject_unknown_keys(loose, _STEP_BODY_KEYS["raw_query"] | {"type"}, where)
        body = loose.get("raw_query")
        if isinstance(body, dict):
            reject_unknown_keys(body, {"sql", "inputs"}, where)
            loose = {**{k: v for k, v in loose.items() if k != "raw_query"}, **body}
        sql = pick(loose, "sql", "raw_query")
        if not isinstance(sql, str) or not sql.strip():
            raise refusal('raw_query needs its SQL as text, e.g. {"raw_query": "SELECT ... FROM input"}.', where, "empty")
        if self.queries is None:
            raise refusal("raw_query is not available: this backend's engine has no query sandbox.", where, "unavailable")
        source = used[0]
        bindings = [self._query_binding(DEFAULT_PLACEHOLDER, source)]
        for placeholder, dataset in self._query_inputs(loose.get("inputs"), where, context, notes, used):
            bindings.append(self._query_binding(placeholder, dataset))
        analysis = analyze_query(sql, bindings, self.queries, where=where, dataset_named=self._dataset_named)
        for written, name in analysis.renamed:
            notes.append(ResolutionNote(f"{where}.columns", written, name, "normalized to snake_case"))
        new_scope = scope.with_fields([
            ScopeField(name=f.name, logical_type=f.logical_type,
                       semantic_role=SemanticRole.MEASURE if f.logical_type.is_numeric else SemanticRole.UNKNOWN)
            for f in analysis.fields
        ])
        datasets = {d.id: d for d in used}
        inputs = [
            QueryInput(
                placeholder=b.placeholder,
                dataset=DatasetRef(dataset_id=b.dataset_id, version=datasets[b.dataset_id].version, name=b.dataset),
                fields=[FieldRef(name=c.name, logical_type=c.logical_type, column_id=c.id)
                        for c in datasets[b.dataset_id].columns],
            )
            for b in bindings
        ]
        step = RawQueryStep(sql=sql, inputs=inputs, columns=analysis.columns, output_schema=new_scope.refs())
        return step, new_scope

    def _query_inputs(self, raw: Any, where: str, context, notes: list[ResolutionNote],
                      used: list[Dataset]) -> list[tuple[str, Dataset]]:
        """The named inputs of a raw_query, each resolved like any dataset reference."""
        if raw is None:
            return []
        if isinstance(raw, dict):
            pairs = list(raw.items())
        elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            pairs = [(item, item) for item in raw]
        else:
            raise refusal('raw_query inputs map placeholder names to datasets, e.g. {"customers": "customers"}.',
                          where, "placeholder_name", name=None)
        bound: dict[str, Dataset] = {}
        for placeholder, reference in pairs:
            name = check_placeholder(placeholder, f"{where}.inputs")
            if name.casefold() in {p.casefold() for p in bound}:
                raise refusal(f"The placeholder {name!r} is bound twice.", f"{where}.inputs", "placeholder_name", name=name)
            try:
                dataset = self.datasets.resolve(reference, field=f"{where}.inputs.{name}", context=context, notes=notes)
            except NotFoundError as err:
                err.details = {**err.details, "raw_query": {"refused": "input_not_found", "name": name,
                                                            "reference": reference if isinstance(reference, str) else None}}
                raise
            if all(d.id != dataset.id for d in used):
                used.append(dataset)
            bound[name] = dataset
        return list(bound.items())

    @staticmethod
    def _query_binding(placeholder: str, dataset: Dataset) -> QueryBinding:
        return QueryBinding(placeholder=placeholder, dataset_id=dataset.id, dataset=dataset.name,
                            columns=[(c.name, c.physical_type) for c in dataset.columns])

    def _dataset_named(self, name: str) -> str | None:
        found = self.datasets.find_exact(name)
        return found.name if found is not None else None

    @staticmethod
    def _check_unique(names: list[str], where: str, *, written: list[Any] | None = None,
                      notes: list[ResolutionNote] | None = None, available: list[str] | None = None) -> None:
        """Refuse a duplicate output name, naming the entries that produce it and any lenient match behind it."""
        seen: set[str] = set()
        for name in names:
            if name in seen:
                entries = [str(w) for w, n in zip(written or [], names) if n == name] if written else []
                lenient = [n for n in notes or [] if n.resolved_to == name and n.reference != name]
                message = f"Duplicate output field {name!r}"
                if lenient and entries:
                    others = [e for e in entries if e not in {n.reference for n in lenient}]
                    message += ": " + " and ".join(
                        [f"{n.reference!r} resolved to {name!r} ({n.reason})" for n in lenient]
                        + [f"{e!r} names it too" for e in others])
                elif len(entries) > 1:
                    message += f": produced by {', '.join(repr(e) for e in entries)}"
                raise InvalidTransformError(
                    message + ".", field=where,
                    details={"name": name, "entries": entries, "resolution": [n.to_dict() for n in lenient],
                             **({"available": available} if available is not None else {})},
                )
            seen.add(name)
