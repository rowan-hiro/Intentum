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
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..contracts import repair_transform, verify_columns, verify_rows
from ..errors import BackendError, ErrorCode
from ..ir import BinaryExpr, CastExpr, ColumnExpr, FilterStep, InExpr, LiteralExpr, TransformIR
from ..models.entities import Column, ContractStatus, Dataset, LogicalType, OutputContract
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
            mapping: dict[str, str] = {}
            for text in equalities:
                left, _, right = text.replace("==", "=").partition("=")
                if _prefix(left) and _prefix(left) == right_name and _prefix(right) != right_name:
                    left, right = right, left
                mapping[_bare(left)] = _bare(right)
            holder["on"] = mapping
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
    if err.code != ErrorCode.NOT_FOUND:
        return None
    reference = arguments.get("dataset") if tool == "describe_dataset" else arguments.get("source")
    if not isinstance(reference, str):
        return None
    lowered = reference.strip().lower()
    if lowered.endswith(_DOCUMENT_SUFFIXES):
        return Advice(
            "document_as_dataset",
            f"{reference!r} is a document, not a dataset. Documents are artifacts: attach_metadata reads a markdown or "
            "text document and attaches the table and column facts it holds to the matching datasets; list_artifacts "
            "shows every document. Datasets are the tables; list_datasets names them.",
            rewrite=[call("attach_metadata", source=reference.strip())],
        )
    if lowered.endswith(_TABULAR_SUFFIXES):
        return Advice(
            "file_as_dataset",
            f"{reference!r} names a file. An imported file is a dataset named after it without the extension (a SQLite "
            "file gives one dataset per table); list_datasets shows the names, import_dataset imports a file that is not there yet.",
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


_DETECTORS: list[Callable[[BackendError, str, dict[str, Any]], Advice | None]] = [
    _subquery, _limit_tail, _like, _distinct, _join_on, _aggregate_in_select, _expression_in_select, _inline_source,
    _unknown_key, _temporal_as_text, _document_as_dataset, _predicate_not_boolean,
]


def advise_error(err: BackendError, *, tool: str, arguments: dict[str, Any]) -> list[Advice]:
    """Advice for a refusal of ``tool`` called with ``arguments``, the loose request as written."""
    if tool == "describe_dataset":
        detectors = [_document_as_dataset]
    elif tool in _TRANSFORM_TOOLS:
        detectors = _DETECTORS
    else:
        return []
    found: list[Advice] = []
    for detector in detectors:
        try:
            advice = detector(err, tool, arguments)
        except Exception:  # advice must never break a response
            advice = None
        if advice is not None and all(a.kind != advice.kind for a in found):
            found.append(advice)
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
