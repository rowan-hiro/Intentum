"""Teach on refusal (MADR 0010).

A refusal that only says what was wrong leaves the agent to guess what the
backend accepts. The set of things an agent may try is open; the language the
backend accepts is small and closed. Every detector here maps one observed
attempt onto the nearest accepted shape and, when the mapping is mechanical,
rewrites the agent's own request into tool calls it can send as-is. Two
signals on successful responses get the same treatment: an empty result whose
filter literal is absent from the filtered column, and a result that already
has (or mechanically reshapes to) the declared output shape.

The module knows the backend's language and the workspace's data. It knows
nothing about the task, the turn budget or the model; that side is the
harness's (MADR 0009). Advice must never break a response: every detector is
guarded, and a detector that cannot rewrite still explains.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from ..contracts import repair_transform, verify_columns, verify_rows
from ..errors import BackendError, ErrorCode
from ..ir import BinaryExpr, CastExpr, ColumnExpr, FilterStep, InExpr, LiteralExpr, TransformIR
from ..ir.expression_parser import parse_expression
from ..models.entities import ArtifactKind, Column, ContractStatus, Dataset, LogicalType, OutputContract
from ..naming import normalize, slugify


@dataclass
class Advice:
    """One thing the agent can do instead.

    ``rewrite`` is the agent's own request, restated as the tool calls that the
    backend accepts, in order; it is present only when the repair is mechanical.
    """

    kind: str
    explanation: str
    rewrite: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"kind": self.kind, "explanation": self.explanation}
        if self.rewrite:
            body["rewrite"] = self.rewrite
        return body


def call(tool: str, **arguments: Any) -> dict[str, Any]:
    """A tool call as the agent would send it; None-valued arguments are omitted."""
    return {"tool": tool, "arguments": {k: v for k, v in arguments.items() if v is not None}}


# ----------------------------------------------------------------------------
# the loose transform language, as much of it as the detectors need
# ----------------------------------------------------------------------------

_TYPE_KEYS = ("type", "op", "operation")
_FILTER_KEYS = ("filter", "where", "predicate", "condition", "expression", "expr")
_SELECT_KEYS = ("select", "fields", "columns")
_GROUP_KEYS = ("group_by", "groupby", "by")
_MEASURE_KEYS = ("measures", "metrics", "metric")
_JOIN_KEYS = ("join", "merge", "semi_join", "semijoin", "where_exists")
_RIGHT_KEYS = ("right", "with", "dataset")
_DATASET_KEYS = ("dataset", "source", "name", "from", "table")
_STEP_KEYS = {"select", "filter", "where", "group_by", "groupby", "measures", "metric", "metrics", "aggregate",
              "sort", "order_by", "sort_by", "limit", "top", "head", "rename", "derive", "compute", "join",
              "semi_join", "semijoin", "where_exists", "columns"}
_AGG_FUNCTIONS = {
    "sum": "sum", "total": "sum", "avg": "avg", "average": "avg", "mean": "avg", "min": "min", "minimum": "min",
    "max": "max", "maximum": "max", "count": "count", "count_distinct": "count_distinct", "nunique": "count_distinct",
    "distinct_count": "count_distinct",
}
_TRANSFORM_TOOLS = ("transform_dataset", "materialize_result")


def _pick(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _steps(transform: Any) -> list[dict[str, Any]]:
    """The transform as a list of step objects (deep-copied), whatever container it came in."""
    if isinstance(transform, dict) and "steps" in transform:
        return [copy.deepcopy(s) for s in transform["steps"] if isinstance(s, dict)]
    if isinstance(transform, list):
        return [copy.deepcopy(s) for s in transform if isinstance(s, dict)]
    if isinstance(transform, dict):
        return [copy.deepcopy(transform)]
    return []


def _rebuild(original: Any, steps: list[dict[str, Any]]) -> Any:
    """Put rewritten steps back in the container shape the agent used."""
    if isinstance(original, dict) and "steps" in original:
        return {"steps": steps}
    if isinstance(original, dict) and len(steps) == 1:
        return steps[0]
    return steps


def _bare(name: str) -> str:
    """``cost.eventid`` → ``eventid``; quoted names are left alone."""
    text = name.strip()
    if text[:1] == '"':
        return text
    return text.rsplit(".", 1)[-1]


def _prefix(name: str) -> str | None:
    text = name.strip()
    if text[:1] == '"' or "." not in text:
        return None
    return text.rsplit(".", 1)[0]


def _expression_texts(step: dict[str, Any]) -> list[tuple[list[Any], str]]:
    """Every expression string in a step with the key path that leads to it."""
    found: list[tuple[list[Any], str]] = []
    for key in _FILTER_KEYS + ("formula",):
        if isinstance(step.get(key), str):
            found.append(([key], step[key]))
    for key in ("derive", "compute"):
        value = step.get(key)
        if isinstance(value, str):
            found.append(([key], value))
        elif isinstance(value, dict):
            for name, expression in value.items():
                if isinstance(expression, str):
                    found.append(([key, name], expression))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str):
                    found.append(([key, index], item))
                elif isinstance(item, dict):
                    for name in ("expression", "expr", "formula"):
                        if isinstance(item.get(name), str):
                            found.append(([key, index, name], item[name]))
    return found


def _set_path(step: dict[str, Any], path: list[Any], value: Any) -> None:
    target: Any = step
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _remove_clause(text: str, span: tuple[int, int]) -> str | None:
    """Remove one clause from an ``and``-conjunction; None when the expression is not a plain conjunction."""
    marker = " __clause__ "
    rest = text[: span[0]] + marker + text[span[1]:]
    if re.search(r"\bor\b", rest, re.IGNORECASE):
        return None
    parts = [p.strip() for p in re.split(r"\s+and\s+", rest.strip(), flags=re.IGNORECASE)]
    parts = [p for p in parts if p and p != "__clause__"]
    residual = " and ".join(parts)
    if "__clause__" in residual or residual.count("(") != residual.count(")"):
        return None
    return residual


def _call_arguments(tool: str, arguments: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """The agent's call with some arguments replaced, keeping only what the tool takes."""
    keep = ("source", "transform", "name", "description", "output_name", "preview_limit")
    merged = {k: v for k, v in arguments.items() if k in keep}
    merged.update(overrides)
    if tool == "transform_dataset":
        merged.pop("name", None)
    return merged


# ----------------------------------------------------------------------------
# detectors on refusals
# ----------------------------------------------------------------------------

_SUBQUERY_RE = re.compile(
    r"""(?P<left>"[^"]+"|[\w.]+)\s+(?P<neg>not\s+)?in\s*\(\s*select\s+(?:distinct\s+)?(?P<col>"[^"]+"|[\w.]+)"""
    r"""\s+from\s+(?P<ds>"[^"]+"|[\w.]+)(?:\s+where\s+(?P<pred>[^()]*(?:\([^()]*\)[^()]*)*))?\s*\)""",
    re.IGNORECASE,
)


def _subquery(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """``x in (select col from ds where ...)`` is a semi_join with a filtered right dataset."""
    steps = _steps(arguments.get("transform"))
    for index, step in enumerate(steps):
        for path, text in _expression_texts(step):
            match = _SUBQUERY_RE.search(text)
            if match is None:
                continue
            left, col, ds = _bare(match.group("left")), _bare(match.group("col")), match.group("ds").strip('"')
            pred = (match.group("pred") or "").strip()
            if match.group("neg"):
                return Advice(
                    "subquery_as_semi_join",
                    f"A subquery is not an expression in this language, and there is no anti-join: 'not in (select ...)' "
                    f"cannot be expressed directly. To keep rows whose {left} does appear in {ds}.{col}, use a semi_join step "
                    f'({{"semi_join": {{"right": "{ds}", "on": {{"{left}": "{col}"}}}}}}).',
                )
            right = ds
            calls: list[dict[str, Any]] = []
            if pred:
                right = slugify(f"{ds}_filtered")
                calls.append(call("materialize_result", source=ds, transform={"filter": pred}, name=right,
                                  description=f"{ds} where {pred}"))
            residual = _remove_clause(text, match.span())
            explanation = (
                f"A subquery is not an expression in this language. To keep rows of the source whose {left} appears in "
                f"{ds}.{col}, add a semi_join step: {{\"semi_join\": {{\"right\": \"{right}\", \"on\": {{\"{left}\": \"{col}\"}}}}}}."
                + (f" A subquery with its own where clause is a filtered right dataset: materialize {ds} with that filter "
                   f"first (as {right}), then semi_join against it." if pred else "")
            )
            if residual is None:
                return Advice("subquery_as_semi_join", explanation)
            new_step = {"semi_join": {"right": right, "on": {left: col}}}
            for key in _TYPE_KEYS:
                step.pop(key, None)
            if residual:
                _set_path(step, path, residual)
                rest = step
            else:
                _set_path(step, path, None)
                rest = _drop_none(step)
            steps[index:index + 1] = [new_step] + ([rest] if rest else [])
            calls.append(call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps))))
            return Advice("subquery_as_semi_join", explanation, rewrite=calls)
    return None


def _drop_none(step: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in step.items() if v is not None}


_LIKE_RE = re.compile(r"""(?P<col>"[^"]+"|[\w.]+)\s+(?P<neg>not\s+)?like\s+'(?P<pat>(?:[^']|'')*)'""", re.IGNORECASE)


def _like(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """``x like '%a%'`` is contains(x, 'a'); a pattern without wildcards is an equality."""
    steps = _steps(arguments.get("transform"))
    seen = False
    rewritable = True
    for step in steps:
        for path, text in _expression_texts(step):
            if not _LIKE_RE.search(text):
                continue
            seen = True

            def replace(match: re.Match[str]) -> str:
                nonlocal rewritable
                col, pat = match.group("col"), match.group("pat")
                core = pat.strip("%")
                if "%" in core or "_" in core:
                    rewritable = False
                    return match.group(0)
                if pat.startswith("%") and pat.endswith("%") and len(pat) >= 2:
                    node = f"contains({col}, '{core}')"
                elif pat.endswith("%"):
                    node = f"starts_with({col}, '{core}')"
                elif pat.startswith("%"):
                    node = f"ends_with({col}, '{core}')"
                else:
                    node = f"{col} = '{core}'"
                return f"not {node}" if match.group("neg") else node

            _set_path(step, path, _LIKE_RE.sub(replace, text))
    if not seen:
        return None
    explanation = ("LIKE is not an operator in this language. Use contains(field, 'text'), starts_with(field, 'text') or "
                   "ends_with(field, 'text') on text fields; a pattern without wildcards is an equality.")
    if not rewritable:
        return Advice("like_as_function", explanation + " A wildcard in the middle of a pattern has no equivalent here.")
    return Advice("like_as_function", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


def _distinct(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A ``distinct`` flag or prefix is a group_by with no measures."""
    steps = _steps(arguments.get("transform"))
    changed = False
    incomplete = False
    for step in steps:
        flagged = "distinct" in step
        select_key = next((k for k in _SELECT_KEYS if k in step), None)
        items = step.get(select_key) if select_key else None
        if isinstance(items, str):
            items = [items]
        prefixed = isinstance(items, list) and any(isinstance(i, str) and i.strip().lower().startswith("distinct ") for i in items)
        if not flagged and not prefixed:
            continue
        step.pop("distinct", None)
        if not isinstance(items, list) or not items:
            incomplete = True
            continue
        names = []
        for item in items:
            if isinstance(item, dict):
                name = _pick(item, "field", "column", "name")
            else:
                name = re.sub(r"^\s*distinct\s+", "", str(item), flags=re.IGNORECASE)
            if name is not None:
                names.append(name)
        step.pop(select_key, None)
        for key in _TYPE_KEYS:
            if str(step.get(key, "")).lower() in ("select", "project", "columns", "keep"):
                step[key] = "aggregate"
        step["group_by"] = names
        changed = True
    if not changed and not incomplete:
        return None
    explanation = ("There is no distinct flag in this language. An aggregate with group_by and no measures returns one row "
                   "per distinct combination of the listed fields, and the result carries exactly those fields.")
    if not changed:
        return Advice("distinct_as_group_by", explanation + " Name the fields to take distinct values of in group_by.")
    return Advice("distinct_as_group_by", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


def _on_mapping(equalities: list[str], right_name: str) -> dict[str, str]:
    """``a = b`` texts as the mapping {left_field: right_field}, oriented by the right dataset's prefix."""
    mapping: dict[str, str] = {}
    for text in equalities:
        left, _, right = text.replace("==", "=").partition("=")
        if _prefix(left) and _prefix(left) == right_name and _prefix(right) != right_name:
            left, right = right, left
        mapping[_bare(left)] = _bare(right)
    return mapping


def _join_on(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """``on: "a = b"`` is the mapping ``{"a": "b"}``."""
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        holders: list[dict[str, Any]] = [step]
        for key in _JOIN_KEYS:
            if isinstance(step.get(key), dict):
                holders.append(step[key])
        for holder in holders:
            raw_on = holder.get("on")
            texts = [raw_on] if isinstance(raw_on, str) else [t for t in raw_on if isinstance(t, str)] if isinstance(raw_on, list) else []
            equalities = [t for t in texts if "=" in t]
            if not equalities:
                continue
            right_name = str(_pick(holder, *_RIGHT_KEYS) or _pick(step, *_RIGHT_KEYS) or "")
            holder["on"] = _on_mapping(equalities, right_name)
            changed = True
    if not changed:
        return None
    return Advice(
        "join_on_as_mapping",
        'join and semi_join take "on" as a mapping from a left field to a right field ({"left_field": "right_field"}), '
        "a shared field name, or a list of either; an equality expression is not parsed there.",
        rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))],
    )


_AGG_ITEM_RE = re.compile(
    r"""^\s*(?P<fn>[a-z_]+)\s*\(\s*(?P<distinct>distinct\s+)?(?P<arg>\*|"[^"]+"|[\w.]+)\s*\)\s*(?:as\s+(?P<alias>"[^"]+"|'[^']+'|\S+))?\s*$""",
    re.IGNORECASE,
)


def _aggregate_in_select(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """``select: ["count(x) as n"]`` is an aggregate step with measures."""
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        select_key = next((k for k in _SELECT_KEYS if k in step), None)
        items = step.get(select_key) if select_key else None
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            continue
        measures: list[dict[str, Any]] = []
        plain: list[Any] = []
        for item in items:
            match = _AGG_ITEM_RE.match(item) if isinstance(item, str) else None
            function = _AGG_FUNCTIONS.get(match.group("fn").lower()) if match else None
            if match is None or function is None:
                plain.append(item)
                continue
            if match.group("distinct"):
                function = "count_distinct"
            measure: dict[str, Any] = {"function": function}
            if match.group("arg") != "*":
                measure["field"] = match.group("arg").strip('"')
            if match.group("alias"):
                measure["alias"] = match.group("alias").strip("\"'")
            measures.append(measure)
        if not measures:
            continue
        group_key = next((k for k in _GROUP_KEYS if k in step), None)
        group_by = step.pop(group_key) if group_key else plain
        if isinstance(group_by, str):
            group_by = [group_by]
        step.pop(select_key, None)
        for key in _TYPE_KEYS:
            if str(step.get(key, "")).lower() in ("select", "project", "columns", "keep"):
                step[key] = "aggregate"
        if group_by:
            step["group_by"] = group_by
        step["measures"] = measures
        changed = True
    if not changed:
        return None
    return Advice(
        "aggregate_as_step",
        "An aggregate function is not a select item. Put it in an aggregate step: group_by lists the fields to group on "
        "and measures lists {function, field, alias} entries; the result carries the group fields and the aliases.",
        rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))],
    )


def _inline_source(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A relation written inline as the source is a dataset to materialize first."""
    source = arguments.get("source")
    if isinstance(source, str) and source.strip()[:1] in "{[":
        try:
            source = json.loads(source)
        except ValueError:
            return None
    if err.code == ErrorCode.INVALID_INTENT and isinstance(err.details.get("received"), (dict, list)):
        source = err.details["received"]
    if isinstance(source, dict) and "right" in source and ("left" in source or "on" in source):
        # A join written as the source: the left side is the source, the join is the first step.
        left = source.get("left") or source.get("from")
        if not isinstance(left, str):
            return Advice("source_as_dataset", "source names one dataset; a join is a step of the transform, "
                          "{\"join\": {\"right\": \"other\", \"on\": {\"left_field\": \"right_field\"}}}.")
        join: dict[str, Any] = {"right": source["right"]}
        on = source.get("on")
        if isinstance(on, (str, list)):
            texts = [on] if isinstance(on, str) else [t for t in on if isinstance(t, str)]
            join["on"] = _on_mapping(texts, str(source["right"])) if any("=" in t for t in texts) else on
        elif on is not None:
            join["on"] = on
        if source.get("how"):
            join["how"] = source["how"]
        steps = [{"join": join}] + _steps(arguments.get("transform"))
        if isinstance(join.get("on"), dict):
            # The right key is never copied into the joined scope: it equals the left key, so a later step
            # that names it gets the left key under that name.
            right_keys = {_bare(str(v)): _bare(str(k)) for k, v in join["on"].items()}
            for step in steps[1:]:
                for key in _SELECT_KEYS + _GROUP_KEYS + ("sort", "order_by", "sort_by"):
                    items = step.get(key)
                    if isinstance(items, list):
                        step[key] = [f"{right_keys[i]} as {i}" if isinstance(i, str) and i in right_keys else i for i in items]
        return Advice(
            "source_as_dataset",
            "source names one dataset (id, name or description); a join is a step of the transform. The left dataset "
            "is the source and the join comes first: {\"join\": {\"right\": \"other\", \"on\": "
            "{\"left_field\": \"right_field\"}}}. After the join, names are unqualified and the right key is not copied.",
            rewrite=[call(tool, **_call_arguments(tool, arguments, source=left, transform=steps))],
        )
    if isinstance(source, list):
        specs = [s for s in source if isinstance(s, dict)]
    elif isinstance(source, dict) and set(source) & _STEP_KEYS:
        specs = [source]
    else:
        return None
    explanation = (
        "source names one dataset (id, name or description). A relation built inline, filtered or projected, is a "
        "dataset of its own: materialize it first, then use its name as the source, or as the right dataset of a join."
    )
    if not specs:
        return Advice("source_as_dataset", explanation)
    first = specs[0]
    ds_key = next((k for k in _DATASET_KEYS if isinstance(first.get(k), str)), None)
    if ds_key is None:
        return Advice("source_as_dataset", explanation)
    ds = first[ds_key]
    body = {k: v for k, v in first.items() if k != ds_key}
    calls: list[dict[str, Any]] = []
    source_name = ds
    if body:
        source_name = slugify(f"{ds}_subset")
        calls.append(call("materialize_result", source=ds, transform=body, name=source_name, description=f"{ds}, restricted"))
    calls.append(call(tool, **_call_arguments(tool, arguments, source=source_name)))
    if len(specs) > 1:
        explanation += f" {len(specs)} inline relations were given; materialize each one and refer to it by name."
    return Advice("source_as_dataset", explanation, rewrite=calls)


_UNKNOWN_KEYS_RE = re.compile(r"Unknown key\(s\) \[(?P<keys>.*?)\]")


def _unknown_key(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """An unknown key is mapped to the nearest accepted one."""
    if err.code != ErrorCode.INVALID_INTENT:
        return None
    match = _UNKNOWN_KEYS_RE.search(err.message)
    if match is None:
        return None
    unknown = [k.strip().strip("'\"") for k in match.group("keys").split(",") if k.strip()]
    unknown = [k for k in unknown if k.lower() != "distinct"]
    if not unknown:
        return None
    allowed = [str(k) for k in (err.details or {}).get("allowed_keys", [])]
    nearest = {k: (difflib.get_close_matches(k.lower(), allowed, n=1, cutoff=0.7) or [None])[0] for k in unknown}
    parts = [f"{k!r} is not a key here" + (f"; the nearest accepted key is {n!r}" if n else "") for k, n in nearest.items()]
    explanation = ". ".join(parts) + "." + (f" Accepted keys: {allowed}." if allowed else "")
    if not all(nearest.values()):
        return Advice("unknown_key", explanation)
    steps = _steps(arguments.get("transform"))

    def rename(node: Any) -> Any:
        if isinstance(node, dict):
            return {nearest.get(k, k): rename(v) for k, v in node.items()}
        if isinstance(node, list):
            return [rename(v) for v in node]
        return node

    steps = [rename(s) for s in steps]
    return Advice("unknown_key", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


_ALIAS_RE = re.compile(r"""^(?P<expr>.+?)\s+as\s+(?P<alias>"[^"]+"|'[^']+'|[^\s"']+)\s*$""", re.IGNORECASE)


def _looks_like_expression(text: str) -> bool:
    return any(ch in text for ch in "()+-*/%<>=|'") and not text.strip().startswith('"')


def _expression_in_select(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """``select: ["strftime(...) as day"]`` is a derive step followed by a select of the new name."""
    steps = _steps(arguments.get("transform"))
    rebuilt: list[dict[str, Any]] = []
    changed = False
    for step in steps:
        select_key = next((k for k in _SELECT_KEYS if k in step), None)
        items = step.get(select_key) if select_key else None
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            rebuilt.append(step)
            continue
        derived: dict[str, str] = {}
        names: list[Any] = []
        for item in items:
            match = _ALIAS_RE.match(item) if isinstance(item, str) else None
            if match and _looks_like_expression(match.group("expr")) and not _AGG_ITEM_RE.match(item):
                alias = match.group("alias").strip("\"'")
                derived[alias] = match.group("expr").strip()
                names.append(alias)
            else:
                names.append(item)
        if not derived:
            rebuilt.append(step)
            continue
        changed = True
        step[select_key] = names
        typed = any(k in step for k in _TYPE_KEYS)
        if not typed and not any(k in step for k in ("derive", "compute")):
            step["derive"] = derived
            rebuilt.append(step)
        else:
            rebuilt.extend([{"derive": derived}, step])
    if not changed:
        return None
    return Advice(
        "expression_as_derive",
        "select takes field names. An expression is computed by a derive step ({\"derive\": {\"name\": \"expression\"}}) "
        "and then selected, grouped or sorted by its name; in a compact object, derive runs before select.",
        rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), rebuilt)))],
    )


_TEMPORAL_AS_TEXT_RE = re.compile(r"has type (timestamp|date), expected string")


def _temporal_as_text(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A string function applied to a date or timestamp: say how dates are computed and how they are rendered."""
    if err.code != ErrorCode.TYPE_MISMATCH or not _TEMPORAL_AS_TEXT_RE.search(err.message):
        return None
    return Advice(
        "temporal_as_text",
        "A date or timestamp is not text, so string functions do not apply to it. To get the calendar day as a typed "
        "date use date(field) or date_trunc('day', field); to compute a text key use strftime(field, '%Y-%m-%d'). "
        "Rendering a value as text in the answer file is export_result's business: pass format_spec with date_format "
        "or timestamp_format there rather than formatting inside a transform.",
    )


_DOCUMENT_SUFFIXES = (".md", ".markdown", ".txt")
_TABULAR_SUFFIXES = (".csv", ".json", ".parquet", ".sqlite", ".sqlite3", ".db")


def _document_as_dataset(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A file named where a dataset is expected: documents are read by attach_metadata, tables by their dataset name."""
    if err.code not in (ErrorCode.NOT_FOUND, ErrorCode.INVALID_SCHEMA):
        return None
    reference = _pick(arguments, "dataset", "source", "path")
    if not isinstance(reference, str) or not reference.strip():
        return None
    reference = reference.strip()
    try:
        kind = ArtifactKind(str(err.details["kind"])) if "kind" in err.details else ArtifactKind.for_suffix(Path(reference).suffix)
    except ValueError:
        kind = ArtifactKind.for_suffix(Path(reference).suffix)
    if kind.is_document:
        return Advice(
            "document_as_dataset",
            f"{reference!r} is a document, not a dataset. attach_metadata reads a markdown or text document for the "
            "table and column facts it states and attaches them to the matching datasets; it does not turn prose or "
            "a table inside the document into rows. Records that live inside a document are extracted outside the "
            "backend and imported as csv, json, parquet or sqlite. list_artifacts shows every document; list_datasets "
            "names the tables.",
            rewrite=[call("attach_metadata", source=reference)],
        )
    if kind in (ArtifactKind.PDF, ArtifactKind.VIDEO, ArtifactKind.AUDIO, ArtifactKind.IMAGE):
        return Advice(
            "media_as_dataset",
            f"{reference!r} is a {kind} file. The backend does not read {kind}; reading it is the host's, outside the "
            "backend. What that reading extracts enters as a dataset through import_dataset (csv, json, parquet, "
            "sqlite) or as facts through attach_metadata (markdown, text). list_artifacts shows the file.",
        )
    if kind.is_tabular:
        return Advice(
            "file_as_dataset",
            f"{reference!r} names a file. An imported file is a dataset named after it without the extension (a SQLite "
            "file gives one dataset per table); list_datasets shows the names, import_dataset imports a file that is not there yet.",
        )
    if err.code == ErrorCode.INVALID_SCHEMA:
        formats = err.details.get("allowed_formats") or ["csv", "parquet", "json", "sqlite"]
        return Advice(
            "unsupported_format",
            f"{reference!r} is not in a format the backend reads as a table. import_dataset takes {', '.join(formats)}; "
            "attach_metadata takes markdown and text; anything else is read outside the backend and brought in as one "
            "of those.",
        )
    return None


def _predicate_not_boolean(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A bare field or arithmetic where a condition was expected."""
    if err.code != ErrorCode.INVALID_TRANSFORM or "predicate must be boolean" not in err.message:
        return None
    return Advice(
        "predicate_not_boolean",
        "filter takes a condition that is true or false for each row, such as patientunitstayid = 2079061, "
        "amount > 100 or region in ('East', 'West'); a bare field name or an arithmetic expression is not a condition. "
        "To keep rows where a field has a value, write \"field is not null\".",
    )


_LIMIT_TAIL_RE = re.compile(r"\s+limit\s+(?P<n>\d+)\s*$", re.IGNORECASE)


def _limit_tail(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """``... LIMIT 5`` at the end of a filter expression is a limit step."""
    steps = _steps(arguments.get("transform"))
    rebuilt: list[dict[str, Any]] = []
    changed = False
    incomplete = False
    for step in steps:
        limit: int | None = None
        for path, text in _expression_texts(step):
            match = _LIMIT_TAIL_RE.search(text)
            if match is None:
                continue
            limit = int(match.group("n"))
            _set_path(step, path, text[: match.start()].strip())
        if limit is None:
            rebuilt.append(step)
            continue
        changed = True
        typed = any(k in step for k in _TYPE_KEYS)
        if any(k in step for k in ("limit", "top", "head")):
            incomplete = True
            rebuilt.append(step)
        elif typed:
            rebuilt.extend([step, {"limit": limit}])
        else:
            step["limit"] = limit
            rebuilt.append(step)
    if not changed:
        return None
    explanation = ("LIMIT is not part of an expression: a filter is only the condition. The number of rows to keep "
                   "is a limit step ({\"limit\": n}), which runs after filter and sort in a compact object.")
    if incomplete:
        return Advice("limit_tail_as_step", explanation + " The step already has a limit; keep one of the two.")
    return Advice("limit_tail_as_step", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), rebuilt)))])


def _declaration_rows(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A declaration that does not say how many rows the answer has, or says it in a shape the contract cannot hold."""
    if err.code != ErrorCode.INVALID_INTENT or err.field != "rows":
        return None
    return Advice(
        "declaration_rows",
        "A declaration says how many rows the answer has: rows=\"one\" for a single value, "
        "rows={\"one_per\": [\"day\"]} for one row for every day, rows=\"at_least_one\" when the number is not "
        "known in advance. A one_per key names the grain, not the payload: it does not have to be a column the "
        "answer carries, and the backend counts by it and leaves it out of the file.",
    )


def _declaration_order(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A declaration whose order_by is not a shape the contract can hold."""
    if err.code != ErrorCode.INVALID_INTENT or err.field != "order_by":
        return None
    return Advice(
        "declaration_order",
        "order_by names the columns the answer is sorted by, as names or {\"column\", \"direction\"} objects: "
        "order_by=[\"treatmentid\"], order_by=[\"-treatmenttime\"] or "
        "order_by=[{\"column\": \"day\", \"direction\": \"desc\"}]. A column the answer is only sorted by does "
        "not belong in columns: the backend sorts by it and leaves it out of the file, as long as the dataset you "
        "export carries it.",
    )


# ----------------------------------------------------------------------------
# detectors that read the facts the raise site attached (details), one per refusal family
# ----------------------------------------------------------------------------

_INFIX_FUNCTION_RE = re.compile(
    r"""(?P<col>\w+\([^()]*\)|"[^"]+"|[\w.]+)\s+(?P<neg>not\s+)?(?P<fn>contains|starts_with|ends_with)\s+(?P<arg>'[^']*'|"[^"]*"|[\w.]+)""",
    re.IGNORECASE,
)


def _function_as_infix(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """``a contains 'x'`` is the function call ``contains(a, 'x')``; the parser has no infix functions."""
    if err.code != ErrorCode.INVALID_TRANSFORM:
        return None
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        for path, text in _expression_texts(step):
            if not _INFIX_FUNCTION_RE.search(text):
                continue

            def replace(match: re.Match[str]) -> str:
                node = f"{match.group('fn').lower()}({match.group('col')}, {match.group('arg')})"
                return f"not {node}" if match.group("neg") else node

            _set_path(step, path, _INFIX_FUNCTION_RE.sub(replace, text))
            changed = True
    if not changed:
        return None
    return Advice(
        "function_as_infix",
        "contains, starts_with and ends_with are functions, not operators: write contains(field, 'text'), "
        "starts_with(field, 'text') or ends_with(field, 'text'); 'not' goes in front of the call.",
        rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))],
    )


def _measure_from_text(item: str) -> dict[str, Any] | None:
    """``max(amount) as peak`` as a measure object, or None when the text is not an aggregate call."""
    match = _AGG_ITEM_RE.match(item)
    function = _AGG_FUNCTIONS.get(match.group("fn").lower()) if match else None
    if match is None or function is None:
        return None
    if match.group("distinct"):
        function = "count_distinct"
    measure: dict[str, Any] = {"function": function}
    if match.group("arg") != "*":
        measure["field"] = match.group("arg").strip('"')
    if match.group("alias"):
        measure["alias"] = match.group("alias").strip("\"'")
    return measure


def _measure_as_object(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A text measure that is an aggregate call becomes a {function, field, alias} object."""
    if err.code != ErrorCode.INVALID_TRANSFORM or not isinstance(err.details.get("received"), str):
        return None
    received = err.details["received"]
    measure = _measure_from_text(received)
    if measure is None:
        return None
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        holders = [step] + [step[k] for k in ("aggregate", "group", "summarize") if isinstance(step.get(k), dict)]
        for holder in holders:
            for key in _MEASURE_KEYS:
                value = holder.get(key)
                if value == received:
                    holder[key] = measure
                    changed = True
                elif isinstance(value, list) and received in value:
                    holder[key] = [measure if v == received else v for v in value]
                    changed = True
    explanation = (
        f"{received!r} is an aggregate call, and a text measure only names a field to sum. An aggregate call is an "
        "object: {\"function\": \"max\", \"field\": \"amount\", \"alias\": \"peak\"}, listed under measures."
    )
    if not changed:
        return Advice("measure_as_object", explanation)
    return Advice("measure_as_object", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


def _measure_needs_field(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A measure without a field: ``*`` only counts; one numeric field in scope is filled in, several are named."""
    if err.code != ErrorCode.INVALID_TRANSFORM or "numeric_fields" not in err.details:
        return None
    numeric = list(err.details.get("numeric_fields") or [])
    received = err.details.get("received")
    function = err.details.get("function")
    steps = _steps(arguments.get("transform"))
    replacement: dict[str, Any] | None = None
    if isinstance(received, dict):
        star = _pick(received, "field", "column", "of")
        if star == "*":
            replacement = {k: v for k, v in received.items() if k not in ("field", "column", "of")}
            for key in ("function", "fn", "agg"):
                if key in replacement:
                    replacement[key] = "count"
        elif len(numeric) == 1:
            replacement = dict(received)
            replacement["field"] = numeric[0]
    changed = False
    if replacement is not None:
        for step in steps:
            holders = [step] + [step[k] for k in ("aggregate", "group", "summarize") if isinstance(step.get(k), dict)]
            for holder in holders:
                for key in _MEASURE_KEYS:
                    value = holder.get(key)
                    if isinstance(value, list) and received in value:
                        holder[key] = [replacement if v == received else v for v in value]
                        changed = True
                    elif value == received:
                        holder[key] = replacement
                        changed = True
    shown = ", ".join(numeric) if numeric else "none"
    explanation = (
        f"{function} needs a field to aggregate; '*' means every row and only count takes it "
        f"({{\"function\": \"count\"}} counts rows). Numeric fields at this step: {shown}."
    )
    if not changed:
        return Advice("measure_needs_field", explanation)
    return Advice("measure_needs_field", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


def _aggregate_body_misplaced(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """An aggregate body ({group_by, measures}) placed under group_by or derive belongs to the aggregate step."""
    if err.code != ErrorCode.INVALID_TRANSFORM:
        return None
    received = err.details.get("received")
    if not isinstance(received, dict) or not (set(received) & set(_GROUP_KEYS + _MEASURE_KEYS)):
        return None
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        for key in _GROUP_KEYS + ("derive", "compute"):
            value = step.get(key)
            body = value if value == received else next((v for v in value if v == received), None) if isinstance(value, list) else None
            if body is None:
                continue
            step.pop(key)
            if isinstance(value, list) and len(value) > 1:
                step[key] = [v for v in value if v != received]
            existing = step.get("aggregate") if isinstance(step.get("aggregate"), dict) else None
            if existing is not None:
                existing.update(body)
            elif any(k in step for k in _GROUP_KEYS + _MEASURE_KEYS):
                step.update(body)
            else:
                step["aggregate"] = dict(body)
            changed = True
    if not changed:
        return None
    return Advice(
        "aggregate_body_misplaced",
        "group_by lists the fields to group on and derive computes a value per row; the body {group_by, measures} "
        "is the aggregate step itself: put it under \"aggregate\", or its two keys at the step level.",
        rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))],
    )


def _literal_retyped(text: str, target: str) -> str | None:
    """A literal as written, re-typed losslessly for ``target``; None when that would change its value."""
    stripped = text.strip()
    if target in ("integer", "float"):
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "'\"":
            inner = stripped[1:-1].strip()
            if re.fullmatch(r"-?\d+", inner) or (target == "float" and re.fullmatch(r"-?\d+\.\d+", inner)):
                return inner
        return None
    if target == "string":
        if re.fullmatch(r"-?\d+(\.\d+)?", stripped):
            return f"'{stripped}'"
        return None
    return None


def _compare_literal_type(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A column compared with a literal of another type: re-type the literal when that loses nothing."""
    if err.code != ErrorCode.TYPE_MISMATCH or "operator" not in err.details:
        return None
    left, right = err.details.get("left") or {}, err.details.get("right") or {}
    if not (isinstance(left, dict) and isinstance(right, dict)) or left.get("literal") == right.get("literal"):
        return None
    column, literal = (left, right) if right.get("literal") else (right, left)
    comparable = err.details.get("comparable_fields") or []
    explanation = (
        f"{column['text']} is {column['type']} and {literal['text']} is {literal['type']}; a comparison needs both sides "
        f"of one type. Fields comparable with {literal['text']}: {', '.join(comparable) if comparable else 'none'}."
    )
    retyped = _literal_retyped(str(literal["text"]), str(column["type"]))
    if retyped is None:
        return Advice("compare_literal_type", explanation + f" {literal['text']} does not read as {column['type']}, "
                      "so the value or the field is the thing to change, not the quotes.")
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        for path, text in _expression_texts(step):
            if literal["text"] in text:
                _set_path(step, path, text.replace(literal["text"], retyped))
                changed = True
    if not changed:
        return Advice("compare_literal_type", explanation)
    return Advice("compare_literal_type", explanation + f" Written as {retyped}, it is {column['type']}.",
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


def _step_index(field: str | None) -> int | None:
    match = re.search(r"steps\[(\d+)\]", field or "")
    return int(match.group(1)) if match else None


def _names_created(step: dict[str, Any]) -> set[str]:
    """Output names a loose step introduces: derived names, measure aliases, rename targets."""
    created: set[str] = set()
    for key in ("derive", "compute"):
        value = step.get(key)
        entries = value if isinstance(value, list) else [value] if value is not None else []
        for entry in entries:
            if isinstance(entry, str):
                match = _ALIAS_RE.match(entry)
                if match:
                    created.add(match.group("alias").strip("\"'"))
            elif isinstance(entry, dict):
                if any(k in entry for k in ("name", "as", "alias", "expression", "expr", "formula")):
                    name = _pick(entry, "name", "as", "alias")
                    if isinstance(name, str):
                        created.add(name)
                    expression = _pick(entry, "expression", "expr", "formula")
                    match = _ALIAS_RE.match(expression) if isinstance(expression, str) else None
                    if match:
                        created.add(match.group("alias").strip("\"'"))
                else:
                    created.update(k for k in entry if isinstance(k, str))
    holders = [step] + [step[k] for k in ("aggregate", "group", "summarize") if isinstance(step.get(k), dict)]
    for holder in holders:
        for key in _MEASURE_KEYS:
            value = holder.get(key)
            for item in (value if isinstance(value, list) else [value] if value is not None else []):
                if isinstance(item, dict):
                    alias = _pick(item, "alias", "as", "name")
                    if isinstance(alias, str):
                        created.add(alias)
                elif isinstance(item, str):
                    match = _AGG_ITEM_RE.match(item)
                    if match and match.group("alias"):
                        created.add(match.group("alias").strip("\"'"))
        rename = holder.get("rename")
        if isinstance(rename, dict):
            created.update(v for v in rename.values() if isinstance(v, str))
    return created


def _field_created_later(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A field named before the step that creates it: steps run in the written order."""
    if err.code != ErrorCode.NOT_FOUND or not isinstance(err.details.get("reference"), str):
        return None
    transform = arguments.get("transform")
    if not isinstance(transform, list):
        return None
    steps = _steps(transform)
    index = _step_index(err.field)
    if index is None or index >= len(steps):
        return None
    reference = err.details["reference"]
    later = [i for i, step in enumerate(steps) if i > index and reference in _names_created(step)]
    if not later:
        return None
    explanation = (
        f"{reference!r} is created in step {later[0] + 1} but used in step {index + 1}; a list of steps runs in the "
        "written order, so a field exists only after the step that derives it. One object with the same keys "
        "orders itself: filter, derive, aggregate, then select, sort and limit."
    )
    singles = [step for step in steps if len(step) == 1 and next(iter(step)) not in _TYPE_KEYS]
    keys = [next(iter(step)) for step in singles]
    if len(singles) != len(steps) or len(set(keys)) != len(keys):
        return Advice("field_created_later", explanation + " Reorder the steps so that the derive comes first.")
    merged: dict[str, Any] = {}
    for step in singles:
        merged.update(step)
    return Advice("field_created_later", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=merged))])


def _join_spec(steps: list[dict[str, Any]]) -> tuple[int, dict[str, Any], dict[str, str], str] | None:
    """The first join step: its index, the holder of ``on``, the on-mapping (left -> right) and the right name."""
    for index, step in enumerate(steps):
        holders: list[dict[str, Any]] = [step]
        holders += [step[k] for k in _JOIN_KEYS if isinstance(step.get(k), dict)]
        typed = str(_pick(step, *_TYPE_KEYS) or "").lower() in ("join", "merge")
        for holder in holders:
            if holder is step and not typed:
                continue
            on = holder.get("on")
            if not isinstance(on, dict):
                continue
            right = str(_pick(holder, *_RIGHT_KEYS) or _pick(step, *_RIGHT_KEYS) or "")
            return index, holder, {str(k): str(v) for k, v in on.items()}, right
    return None


def _join_scope_names(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """After a join, names are unqualified and a right-side key is not copied: it equals the left key."""
    if err.code not in (ErrorCode.NOT_FOUND, ErrorCode.INVALID_TRANSFORM):
        return None
    steps = _steps(arguments.get("transform"))
    spec = _join_spec(steps)
    if spec is None:
        return None
    join_index, _, on, right_name = spec
    right_keys = {_bare(v): _bare(k) for k, v in on.items()}  # right key -> left key
    available = set(err.details.get("available") or [])
    lenient = {n.get("reference"): n for n in err.details.get("resolution") or [] if isinstance(n, dict)}
    source = str(arguments.get("source") or "")
    reasons: list[str] = []
    changed = False

    def fix(item: Any) -> Any:
        nonlocal changed
        if not isinstance(item, str):
            return item
        text = item.strip()
        head, alias = text, None
        match = _ALIAS_RE.match(text)
        if match:
            head, alias = match.group("expr").strip(), match.group("alias").strip("\"'")
        prefix = _prefix(head)
        if prefix and prefix in (right_name, source) and (_bare(head) in available or not available):
            bare = _bare(head)
            reasons.append(f"{text!r} is {bare!r} here: names are unqualified after a join")
            changed = True
            return f"{bare} as {alias}" if alias and alias != bare else bare
        if head in right_keys and head not in available:
            left = right_keys[head]
            note = lenient.get(head)
            why = f" ({head!r} had been read as {note['resolved_to']!r} by a {note['reason']})" if note else ""
            reasons.append(f"{head!r} is the join key on the right side and is not copied: it equals {left!r}{why}")
            changed = True
            return f"{left} as {alias or head}"
        return item

    for step in steps[join_index:]:
        for key in _SELECT_KEYS + _GROUP_KEYS + ("sort", "order_by", "sort_by"):
            value = step.get(key)
            if isinstance(value, list):
                step[key] = [fix(v) for v in value]
            elif isinstance(value, str) and key not in _GROUP_KEYS:
                step[key] = fix(value)
    if not changed:
        return None
    return Advice(
        "join_scope_names",
        "; ".join(reasons) + ". Select the value under the name you want with 'field as name'.",
        rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))],
    )


def _field_not_in_scope(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """The fields at this step, and the names that merely look alike, named as names; never a rewrite."""
    if err.code != ErrorCode.NOT_FOUND or not isinstance(err.details.get("reference"), str):
        return None
    reference = err.details["reference"]
    available = list(err.details.get("available") or [])
    close = [c for c in err.details.get("close") or [] if c != reference]
    index = _step_index(err.field)
    where = f"step {index + 1}" if index is not None else "this step"
    text = f"{reference!r} is not a field at {where}; the fields there are {', '.join(available) if available else 'none'}."
    if close:
        text += (f" {', '.join(repr(c) for c in close)} look alike but are other fields; use one only if it is the "
                 "field you mean.")
    text += (" A name that a preview computed exists only in that preview: materialize_result keeps it, "
             "or derive it again in this transform.")
    return Advice("field_not_in_scope", text)


def _document_not_found(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """attach_metadata on a name no document has: the documents that exist, and the one match when there is one."""
    if err.code != ErrorCode.NOT_FOUND or err.field != "source" or "documents" not in err.details:
        return None
    documents = [str(d) for d in err.details.get("documents") or []]
    reference = str(err.details.get("reference") or arguments.get("source") or "")
    explanation = (f"No document is registered as {reference!r}. Documents here: "
                   f"{', '.join(documents) if documents else 'none'}; list_artifacts shows them all, and a path to "
                   "a markdown or text file outside the workspace is accepted too.")
    if not documents:
        return Advice("document_not_found", explanation)
    target: str | None = None
    if len(documents) == 1:
        target = documents[0]
    else:
        by_file = {d.rsplit("/", 1)[-1].lower(): d for d in documents}
        matches = difflib.get_close_matches(reference.rsplit("/", 1)[-1].lower(), list(by_file), n=2, cutoff=0.6)
        if len(matches) == 1:
            target = by_file[matches[0]]
    if target is None:
        return Advice("document_not_found", explanation)
    others = {k: v for k, v in arguments.items() if k in ("dataset", "overwrite") and v is not None}
    return Advice("document_not_found", explanation, rewrite=[call("attach_metadata", source=target, **others)])


def _file_exists(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """An export onto an existing file: what is there, and that overwriting replaces it for good."""
    if err.code != ErrorCode.CONFLICT or err.field != "path" or "path" not in err.details:
        return None
    existing = err.details.get("existing") or {}
    facts = ""
    if existing:
        facts = f" ({existing.get('size_bytes')} bytes, modified {existing.get('modified_at')})"
    explanation = (
        f"{err.details['path']} already exists{facts}, and export_result leaves an existing file alone unless "
        "overwrite=true. Sending the same call with overwrite=true replaces that file; the backend keeps no copy of "
        "the earlier content. The dataset is untouched either way and can be exported to another path."
    )
    keep = ("dataset", "path", "format", "format_spec")
    same = {k: v for k, v in arguments.items() if k in keep and v is not None}
    if "dataset" not in same or "path" not in same:
        return Advice("file_exists", explanation)
    return Advice("file_exists", explanation, rewrite=[call("export_result", **same, overwrite=True)])


_ACROSS_ROWS_RE = re.compile(r"^(max|min|latest|last|first|earliest|total|sum|count|avg|mean|average)[_\s]?", re.IGNORECASE)


def _derive_without_expression(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A derive with a name and nothing to compute: what derive is for, and where a value across rows lives."""
    if err.code != ErrorCode.INVALID_TRANSFORM or not isinstance(err.details.get("shape"), dict):
        return None
    received = err.details.get("received")
    name = received.get("name") if isinstance(received, dict) else None
    available = list(err.details.get("available") or [])
    text = (f"derive computes one value per row from an expression over the fields at this step "
            f"({', '.join(available) if available else 'none'}), as {{\"name\": \"total\", \"expression\": "
            f"\"quantity * unit_price\"}}; ")
    text += f"{name!r} names a result but gives nothing to compute. " if isinstance(name, str) else "nothing to compute was given. "
    text += ("A value across rows, such as a maximum, a latest time or a count, is not a derive: it is an aggregate step, "
             "{\"aggregate\": {\"measures\": [{\"function\": \"max\", \"field\": \"time\", \"alias\": \"max_time\"}]}}. "
             "To keep the rows at that maximum, materialize the aggregate and semi_join the source on it "
             "({\"semi_join\": {\"right\": \"peak\", \"on\": {\"time\": \"max_time\"}}}), or sort by the field "
             "descending and limit when one row is enough.")
    if isinstance(name, str) and _ACROSS_ROWS_RE.match(name):
        text += f" The name {name!r} reads like such a value."
    return Advice("derive_without_expression", text)


def _stray_characters(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """Characters after a complete expression that belong to no token: drop them when the rest parses."""
    if err.code != ErrorCode.INVALID_TRANSFORM or "position" not in err.details or "expression" not in err.details:
        return None
    if not str(err.message).startswith("Unexpected character"):
        return None
    text = str(err.details["expression"])
    position = int(err.details["position"])
    head, tail = text[:position], text[position:]
    if not head.strip() or not tail or re.search(r"[\x21-\x7e]", tail):
        return None  # the tail holds printable ASCII that could be expression text; not ours to cut
    try:
        parse_expression(head)
    except BackendError:
        return None
    explanation = (f"The expression is complete at position {position}; what follows, {tail!r}, is not part of any "
                   "token and looks like stray output. The same expression without it:")
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        for path, value in _expression_texts(step):
            if value == text:
                _set_path(step, path, head.rstrip())
                changed = True
    if not changed:
        return Advice("stray_characters", explanation.rstrip(":") + ".")
    return Advice("stray_characters", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


def _name_taken(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A materialization name that another request already produced: what is there, and the two ways on."""
    if err.code != ErrorCode.CONFLICT or err.field != "name" or not isinstance(err.details.get("existing"), dict):
        return None
    existing = err.details["existing"]
    name = existing.get("name") or arguments.get("name")
    rows = existing.get("rows")
    facts = f"{name!r} ({existing.get('id')}" + (f", {rows} rows" if rows is not None else "") + ")"
    return Advice(
        "name_taken",
        f"A dataset {facts} already exists and came from a different request, so this one is not a retry of it. "
        "Either use that dataset by its name if it is the result you want (describe_dataset shows it), or "
        "materialize under another name; delete_dataset frees the name and restore_dataset brings it back.",
    )


def _transform_as_text(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A transform sent as text: the object it parses to, or where the text stops being JSON."""
    transform = arguments.get("transform")
    if not isinstance(transform, str):
        return None
    try:
        parsed = json.loads(transform)
    except ValueError as exc:
        return Advice(
            "transform_as_text",
            f"transform is an object or a list of steps, not text. The text sent is not JSON: {exc}. "
            "Send the structure itself; a string is not parsed.",
        )
    if not isinstance(parsed, (dict, list)):
        return Advice("transform_as_text", "transform is an object or a list of steps, not text.")
    return Advice(
        "transform_as_text",
        "transform is an object or a list of steps; it was sent as text. The same request with the structure itself:",
        rewrite=[call(tool, **_call_arguments(tool, arguments, transform=parsed))],
    )


Detector = Callable[[BackendError, str, dict[str, Any]], Advice | None]

# Tools whose refusals may name a document or a file where a dataset was expected.
_DATASET_TOOLS = _TRANSFORM_TOOLS + ("describe_dataset", "get_provenance", "publish_dataset", "update_metadata",
                                     "delete_dataset", "restore_dataset", "export_result")

# Every detector, the tools it applies to (None for every tool), and whether it is a fallback: a fallback
# runs only when no other detector recognised the refusal, so a general explanation never stacks on a
# specific one. Order is the order advice is listed in.
_REGISTRY: list[tuple[Detector, tuple[str, ...] | None, bool]] = [
    (_subquery, _TRANSFORM_TOOLS, False),
    (_limit_tail, _TRANSFORM_TOOLS, False),
    (_like, _TRANSFORM_TOOLS, False),
    (_distinct, _TRANSFORM_TOOLS, False),
    (_join_on, _TRANSFORM_TOOLS, False),
    (_aggregate_in_select, _TRANSFORM_TOOLS, False),
    (_expression_in_select, _TRANSFORM_TOOLS, False),
    (_inline_source, _TRANSFORM_TOOLS, False),
    (_unknown_key, _TRANSFORM_TOOLS, False),
    (_temporal_as_text, _TRANSFORM_TOOLS, False),
    (_document_as_dataset, _DATASET_TOOLS + ("import_dataset",), False),
    (_predicate_not_boolean, _TRANSFORM_TOOLS, False),
    (_declaration_rows, ("declare_output",), False),
    (_declaration_order, ("declare_output",), False),
    # detectors that read the facts the raise site attached
    (_transform_as_text, _TRANSFORM_TOOLS, False),
    (_function_as_infix, _TRANSFORM_TOOLS, False),
    (_measure_as_object, _TRANSFORM_TOOLS, False),
    (_measure_needs_field, _TRANSFORM_TOOLS, False),
    (_aggregate_body_misplaced, _TRANSFORM_TOOLS, False),
    (_compare_literal_type, _TRANSFORM_TOOLS, False),
    (_join_scope_names, _TRANSFORM_TOOLS, False),
    (_field_created_later, _TRANSFORM_TOOLS, False),
    (_document_not_found, ("attach_metadata",), False),
    (_file_exists, ("export_result",), False),
    (_name_taken, _TRANSFORM_TOOLS, False),
    (_derive_without_expression, _TRANSFORM_TOOLS, False),
    (_stray_characters, _TRANSFORM_TOOLS, False),
    (_field_not_in_scope, _TRANSFORM_TOOLS, True),
]


def advise_error(err: BackendError, *, tool: str, arguments: dict[str, Any]) -> list[Advice]:
    """Advice for a refusal of ``tool`` called with ``arguments``, the loose request as written."""
    found: list[Advice] = []

    def consider(detector: Detector) -> None:
        try:
            advice = detector(err, tool, arguments)
        except Exception:  # advice must never break a response
            advice = None
        if advice is not None and all(a.kind != advice.kind for a in found):
            found.append(advice)

    applicable = [(detector, fallback) for detector, tools, fallback in _REGISTRY if tools is None or tool in tools]
    for detector, fallback in applicable:
        if not fallback:
            consider(detector)
    if not found:
        for detector, fallback in applicable:
            if fallback:
                consider(detector)
    return found


# ----------------------------------------------------------------------------
# signals on successful results
# ----------------------------------------------------------------------------

def _literals(ir: TransformIR) -> list[tuple[str, Any]]:
    """``(column_id, value)`` for every equality or membership test against a literal in the filters."""
    found: list[tuple[str, Any]] = []

    def literal_value(node: Any) -> Any:
        if isinstance(node, CastExpr):
            node = node.expr
        if isinstance(node, LiteralExpr) and isinstance(node.value, (int, str)) and not isinstance(node.value, bool):
            return node.value
        return None

    def walk(node: Any) -> None:
        if isinstance(node, BinaryExpr):
            if node.op == "=":
                for column, other in ((node.left, node.right), (node.right, node.left)):
                    value = literal_value(other)
                    if isinstance(column, ColumnExpr) and column.field.column_id and value is not None:
                        found.append((column.field.column_id, value))
            elif node.op in ("and", "or"):
                walk(node.left)
                walk(node.right)
        elif isinstance(node, InExpr) and not node.negated and isinstance(node.expr, ColumnExpr) and node.expr.field.column_id:
            for item in node.values[:3]:
                value = literal_value(item)
                if value is not None:
                    found.append((node.expr.field.column_id, value))

    for step in ir.steps:
        if isinstance(step, FilterStep):
            walk(step.predicate)
    return found[:4]


def _fits(column: Column, value: Any) -> bool:
    if isinstance(value, str):
        return column.logical_type == LogicalType.STRING
    return column.logical_type in (LogicalType.INTEGER, LogicalType.FLOAT)


def advise_empty_result(
    ir: TransformIR,
    *,
    used: list[Dataset],
    workspace: list[Dataset],
    occurs: Callable[[Dataset, Column, Any], bool],
    max_probes: int = 64,
) -> Advice | None:
    """An empty result whose filter literal is absent from its column: say where the literal does occur."""
    by_column: dict[str, tuple[Dataset, Column]] = {c.id: (d, c) for d in used for c in d.columns}
    checks = [(by_column[cid], value) for cid, value in _literals(ir) if cid in by_column]
    if not checks:
        return None
    present: list[str] = []
    absent: list[str] = []
    probes = 0
    for (dataset, column), value in checks:
        where = f"{dataset.name}.{column.name}"
        if occurs(dataset, column, value):
            present.append(f"{value!r} occurs in {where}")
            continue
        candidates = [
            (d, c) for d in workspace for c in d.columns
            if (d.id, c.name) != (dataset.id, column.name) and _fits(c, value)
        ]
        candidates.sort(key=lambda dc: -difflib.SequenceMatcher(None, normalize(dc[1].name), normalize(column.name)).ratio())
        hits: list[str] = []
        for d, c in candidates:
            if probes >= max_probes or len(hits) >= 5:
                break
            probes += 1
            if occurs(d, c, value):
                hits.append(f"{d.name}.{c.name}")
        if hits:
            absent.append(f"{value!r} does not occur in {where}; it occurs in {', '.join(hits)}")
        else:
            absent.append(f"{value!r} does not occur in {where}, nor in any comparable column of the workspace")
    if not absent:
        return Advice("no_matching_rows",
                      "The result is empty although each filtered value occurs in its column (" + "; ".join(present)
                      + "); the conditions together match no row.")
    return Advice("value_not_found",
                  "The result is empty. " + "; ".join(absent + present)
                  + ". A value that lives in another column is usually a different identifier: relate the datasets "
                    "through the column that holds it, rather than filtering this one by it.")


def _append_steps(transform: Any, repair: dict[str, Any]) -> Any:
    steps = _steps(transform)
    if "rename" in repair:
        steps.append({"rename": repair["rename"]})
    if "select" in repair:
        steps.append({"select": repair["select"]})
    return steps


def advise_contract(
    ir: TransformIR,
    *,
    contract: OutputContract,
    row_count: int,
    tool: str,
    arguments: dict[str, Any],
    materialized_name: str | None,
) -> Advice | None:
    """A result that has, or mechanically reshapes to, the declared output shape: say so, with the next call."""
    if contract.status != ContractStatus.OPEN:
        return None
    actual = [(f.name, f.logical_type) for f in ir.output_schema]
    problems = verify_columns(contract, actual) + verify_rows(contract, row_count, None)
    shape = f"columns [{', '.join(c.name for c in contract.columns)}]" + (f", rows {contract.rows}" if contract.rows else "")
    answer = f"answer_{contract.id}"
    if not problems:
        if materialized_name:
            return Advice("matches_contract",
                          f"{materialized_name} has the shape declared in output contract {contract.id} ({shape}); "
                          f"export_result(dataset={materialized_name!r}, path=...) will accept it.")
        return Advice(
            "matches_contract",
            f"This preview has the shape declared in output contract {contract.id} ({shape}). Materialize it and export "
            "the dataset; export_result will accept it.",
            rewrite=[call("materialize_result", source=arguments.get("source"), transform=arguments.get("transform"),
                          name=answer, description=contract.description or "the declared deliverable")],
        )
    repair = repair_transform(contract, problems)
    if repair is None:
        return None
    if materialized_name:
        rewrite = [call("materialize_result", source=materialized_name, transform=repair, name=answer,
                        description=contract.description or "the declared deliverable")]
    else:
        rewrite = [call("materialize_result", source=arguments.get("source"),
                        transform=_append_steps(arguments.get("transform"), repair), name=answer,
                        description=contract.description or "the declared deliverable")]
    return Advice(
        "near_contract",
        f"This result differs from output contract {contract.id} ({shape}) only in shape: "
        + " ".join(p.message for p in problems) + " Reshape it as below and export the new dataset.",
        rewrite=rewrite,
    )
