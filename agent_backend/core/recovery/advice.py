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
from ..models.entities import ArtifactKind, Column, ContractStatus, Dataset, LogicalType, OutputContract, RowCardinality
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
              "semi_join", "semijoin", "where_exists", "columns", "raw_query"}
_QUERY_KEYS = ("raw_query", "sql")
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


_LIKE_RE = re.compile(r"""(?P<col>"[^"]+"|\w+\([^()]*\)|[\w.]+)\s+(?P<neg>not\s+)?(?P<op>i?like)\s+'(?P<pat>(?:[^']|'')*)'""",
                      re.IGNORECASE)


def _like(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """``x like '%a%'`` is contains(x, 'a'); a pattern without wildcards is an equality; ``ilike`` compares lower-cased."""
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
                if match.group("op").lower() == "ilike":
                    col, pat = f"lower({col})", pat.lower()
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
    explanation = ("LIKE and ILIKE are not operators in this language. Use contains(field, 'text'), starts_with(field, "
                   "'text') or ends_with(field, 'text') on text fields, on lower(field) for a case-insensitive match; a pattern "
                   "without wildcards is an equality.")
    if not rewritable:
        return Advice("like_as_function", explanation + " A wildcard in the middle of a pattern has no equivalent here.")
    return Advice("like_as_function", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


_EXTRACT_RE = re.compile(r"\bextract\s*\(\s*(year|month|day)\s+from\s+", re.IGNORECASE)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _outside_literals(text: str, respell: Callable[[str], str | None]) -> str | None:
    """Apply ``respell`` to an expression with its single-quoted literals held aside, so their text is kept as written."""
    literals: list[str] = []

    def hold(match: re.Match[str]) -> str:
        literals.append(match.group(0))
        return f"\x00{len(literals) - 1}\x00"

    masked = respell(_STRING_LITERAL_RE.sub(hold, text))
    return None if masked is None else re.sub(r"\x00(\d+)\x00", lambda m: literals[int(m.group(1))], masked)


def _respelled(text: str) -> str | None:
    """SQL spellings with a direct equivalent here: backtick identifiers, and EXTRACT of a year, month or day."""
    return _outside_literals(text, _respell_code)


def _respell_code(text: str) -> str | None:
    out = re.sub(r'`"([^"`]+)"`', r'"\1"', text)
    out = re.sub(r"`([^`]+)`", lambda m: '"' + m.group(1).replace('"', '""') + '"', out)
    while (match := _EXTRACT_RE.search(out)) is not None:
        depth, end = 1, match.end()
        while end < len(out) and depth:
            depth += {"(": 1, ")": -1}.get(out[end], 0)
            end += 1
        if depth:
            return None
        out = out[:match.start()] + f"{match.group(1).lower()}({out[match.end():end - 1].strip()})" + out[end:]
    return out


def _sql_spelling(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """Backtick identifiers and EXTRACT(part FROM x) have direct spellings here: "name" and year(x), month(x), day(x)."""
    if err.code != ErrorCode.INVALID_TRANSFORM or "expression" not in err.details:
        return None
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        for path, text in _expression_texts(step):
            code = _STRING_LITERAL_RE.sub("''", text)
            if "`" not in code and not _EXTRACT_RE.search(code):
                continue
            respelled = _respelled(text)
            if respelled is None or respelled == text:
                continue
            try:
                parse_expression(respelled)
            except BackendError:
                continue
            _set_path(step, path, respelled)
            changed = True
    if not changed:
        return None
    return Advice("sql_spelling",
                  "This language quotes names with double quotes, not backticks, and writes EXTRACT(YEAR FROM x) as "
                  "year(x) (likewise month and day). The same request respelled:",
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=_rebuild(arguments.get("transform"), steps)))])


_SQL_ONLY = (("CASE", re.compile(r"\bcase\b[\s\S]*\bwhen\b", re.IGNORECASE)),
             ("a window function", re.compile(r"\bover\s*\(", re.IGNORECASE)),
             ("a subquery", re.compile(r"\(\s*select\b", re.IGNORECASE)))


def _sql_in_expression(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """CASE, a window function or a scalar subquery: SQL the steps lack, which a raw_query first step runs (MADR 0002)."""
    if err.code != ErrorCode.INVALID_TRANSFORM or "expression" not in err.details:
        return None
    text = str(err.details["expression"])
    code = _STRING_LITERAL_RE.sub("''", text)
    found = [name for name, pattern in _SQL_ONLY if pattern.search(code)]
    if not found or re.search(r"\bin\s*\(\s*select\b", code, re.IGNORECASE):
        return None  # IN (SELECT ...) is a semi_join; _subquery says so
    named = " and ".join(found)
    explanation = (f"{named[0].upper()}{named[1:]} is SQL that semantic expressions do not have. A raw_query first "
                   "step runs it: one read-only SELECT in which input is the source, and other datasets are bound by "
                   "name under inputs; semantic steps may follow it.")
    tables = {name.strip('"').lower() for name in re.findall(r"\bfrom\s+(\"[^\"]+\"|[\w.]+)", code, re.IGNORECASE)}
    if tables - {"input"}:
        return Advice("sql_in_expression", explanation + " The subquery reads other datasets, which the query binds "
                                                          "under inputs by name.")
    # Only the request's first step, as the backend normalized it, can become the query without reordering what
    # runs before it; a filter or join ahead of it would otherwise run after it.
    resolution = err.resolution or {}
    steps, index = resolution.get("steps"), resolution.get("index")
    query = _step_as_query(steps[0], text, found) if isinstance(steps, list) and steps and index == 0 else None
    if query is None:
        return Advice("sql_in_expression", explanation + " Written in the transform's first step, a filter or a "
                                                          "derive, it is rewritten as that query.")
    rewritten = [{"raw_query": {"sql": query}}, *copy.deepcopy(steps[1:])]
    return Advice("sql_in_expression", explanation + " The same request with that step as the query:",
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=rewritten))])


def _step_as_query(step: dict[str, Any], text: str, found: list[str]) -> str | None:
    """A normalized filter or single derive whose expression is ``text``, as the equivalent query over input."""
    kind = str(step.get("type") or "").lower()
    body = next((step[k] for k in (kind, "compute") if isinstance(step.get(k), dict)), step)
    if kind == "filter":
        expression = next((v for k, v in {**step, **body}.items() if k in _FILTER_KEYS and isinstance(v, str)), None)
        if expression != text:
            return None
        clause = "QUALIFY" if "a window function" in found else "WHERE"  # a window is filtered after it is computed
        return f"SELECT * FROM input {clause} {text}"
    if kind == "derive":
        if isinstance(body.get("name"), str) and body.get("expression") == text:
            name = body["name"]
        elif len(body) == 1 and next(iter(body.values())) == text:
            name = next(iter(body))
        else:
            return None
        return f'SELECT *, {text} AS "{name}" FROM input'
    return None


_CAST_CALL_RE = re.compile(r"(?<![\w.])cast\s*\(", re.IGNORECASE)
_POSTFIX_CAST_RE = re.compile(r"""(?P<operand>"[^"]+"|\w+\([^()]*\)|[\w.]+)::(?P<type>\w+(?:\s*\([\d,\s]+\))?)""")


def _cast_conversion(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A cast that met a value it cannot convert failed the transform; try_cast yields null for such a value."""
    if err.code != ErrorCode.EXECUTION_FAILED or "Conversion Error" not in str(err.message) or "raw_query" in err.details:
        return None
    explanation = ("A cast met a value it cannot convert, so the transform failed. try_cast(x as type) yields null for "
                   "such a value instead; filter the nulls out or keep them, as the request needs.")
    steps = _steps(arguments.get("transform"))
    changed = False
    for step in steps:
        for path, text in _expression_texts(step):
            safe = _outside_literals(text, lambda code: _CAST_CALL_RE.sub("try_cast(", _POSTFIX_CAST_RE.sub(
                lambda m: f"try_cast({m.group('operand')} as {m.group('type')})", code)))
            if safe is None or safe == text or "::" in _STRING_LITERAL_RE.sub("", safe):
                continue
            try:
                parse_expression(safe)
            except BackendError:
                continue
            _set_path(step, path, safe)
            changed = True
    if not changed:
        return Advice("cast_conversion", explanation)
    return Advice("cast_conversion", explanation + " The same request with try_cast:",
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


_LEFT_KEYS = ("left", "input", "from", "source", "dataset", "base")
_SOURCE_IS_ONE = "source names one dataset (id, name or description)"


def _joins_right(steps: list[dict[str, Any]], right: str) -> bool:
    """Whether a step already joins ``right``."""
    for step in steps:
        body = _pick(step, *_JOIN_KEYS)
        kind = _pick(step, *_TYPE_KEYS)
        if not isinstance(body, dict) and isinstance(kind, str) and kind.lower() in _JOIN_KEYS:
            body = step
        if isinstance(body, dict) and str(_pick(body, *_RIGHT_KEYS)) == right:
            return True
    return False


def _query_source(inputs: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """The source a query over ``inputs`` reads, and the inputs left to bind.

    A binding named ``input`` is the source's, so it leaves the inputs; otherwise the first dataset is the source and
    stays bound under its placeholder too, which raw_query accepts as reading the source.
    """
    named = next((k for k in inputs if k.casefold() == "input"), None)
    if named is not None and isinstance(inputs[named], str):
        return inputs[named], {k: v for k, v in inputs.items() if k != named}
    first = next((v for v in inputs.values() if isinstance(v, str)), None)
    return first, dict(inputs)


def _inline_source(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A relation written inline as the source is a dataset to materialize first, or a step of the transform."""
    source = arguments.get("source")
    if isinstance(source, str) and source.strip()[:1] in "{[":
        try:
            source = json.loads(source)
        except ValueError:
            return None
    if err.code == ErrorCode.INVALID_INTENT and isinstance(err.details.get("received"), (dict, list)):
        source = err.details["received"]
    steps = _steps(arguments.get("transform"))
    if isinstance(source, dict) and any(k in source for k in _QUERY_KEYS):
        # A query written as the source: its SQL reads the source as input, and the others under inputs.
        sql, inputs = _query_parts(source)
        explanation = (f"{_SOURCE_IS_ONE}; a query is a raw_query first step of the transform. Its SQL reads the "
                       "source as input, and every other dataset by the name inputs binds it to.")
        if sql is None:
            return Advice("source_as_dataset", explanation)
        dataset, bound = _query_source(inputs)
        if dataset is None:
            return Advice("source_as_dataset", explanation + " Name the dataset the SQL reads as the source and read "
                          "it as input in the SQL; a physical table name is not a placeholder.")
        query = {"sql": sql}
        if bound:
            query["inputs"] = bound
        return Advice("source_as_dataset", explanation + f" Here {dataset} is the source and the query is the first step.",
                      rewrite=[call(tool, **_call_arguments(tool, arguments, source=dataset,
                                                            transform=[{"raw_query": query}] + steps))])
    if isinstance(source, dict) and "right" in source:
        # A join written as the source: the left side is the source, the join is the first step.
        left = next((source[k] for k in _LEFT_KEYS if isinstance(source.get(k), str)), None)
        right = str(source["right"])
        shape = "{\"join\": {\"right\": \"other\", \"on\": {\"left_field\": \"right_field\"}}}"
        if left is None:
            return Advice("source_as_dataset", f"{_SOURCE_IS_ONE}; a join is a step of the transform, {shape}. "
                          "Name the left dataset as the source.")
        if _joins_right(steps, right):
            return Advice("source_as_dataset", f"{_SOURCE_IS_ONE}: the left dataset, {left}. The transform already "
                          f"joins {right}.", rewrite=[call(tool, **_call_arguments(tool, arguments, source=left))])
        if source.get("on") is None:
            return Advice("source_as_dataset", f"{_SOURCE_IS_ONE}: the left dataset, {left}. The join is the first "
                          f"step of the transform, {shape}, and needs the fields it joins on.")
        join: dict[str, Any] = {"right": source["right"]}
        on = source.get("on")
        if isinstance(on, (str, list)):
            texts = [on] if isinstance(on, str) else [t for t in on if isinstance(t, str)]
            join["on"] = _on_mapping(texts, right) if any("=" in t for t in texts) else on
        else:
            join["on"] = on
        if source.get("how"):
            join["how"] = source["how"]
        steps = [{"join": join}] + steps
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
            f"{_SOURCE_IS_ONE}; a join is a step of the transform. The left dataset "
            f"is the source and the join comes first: {shape}. After the join, names are unqualified and the right "
            "key is not copied.",
            rewrite=[call(tool, **_call_arguments(tool, arguments, source=left, transform=steps))],
        )
    if isinstance(source, dict) and source and not set(source) & (_STEP_KEYS | set(_DATASET_KEYS) | set(_LEFT_KEYS)):
        values = list(source.values())
        if len(source) == 1 and isinstance(values[0], dict) and set(values[0]) & _STEP_KEYS:
            # {"dataset": {steps}}: the key is the source and the value its transform.
            (name,) = source
            return Advice("source_as_dataset", f"{_SOURCE_IS_ONE}, here {name}; the steps applied to it are the "
                          "transform.", rewrite=[call(tool, **_call_arguments(tool, arguments, source=name,
                                                                                transform=_steps(values[0]) + steps))])
        if all(isinstance(v, str) for v in values):
            # {"placeholder": "dataset", ...}: the inputs of a query.
            found = _query_step(arguments.get("transform"))
            if found is None:
                return Advice("source_as_dataset", f"{_SOURCE_IS_ONE}. Several datasets are read by a join step, or "
                              "by a raw_query first step whose inputs binds each of the others to a placeholder.")
            steps, index, step = found
            sql, inputs = _query_parts(step)
            dataset, bound = _query_source({**source, **inputs})
            steps[index] = _query_restated(step, sql or "", bound)
            return Advice("source_as_dataset", f"{_SOURCE_IS_ONE}, here {dataset}; the other datasets a raw_query "
                          "reads are bound under its inputs.",
                          rewrite=[call(tool, **_call_arguments(tool, arguments, source=dataset,
                                                                transform=_rebuild(arguments.get("transform"), steps)))])
    if isinstance(source, list):
        specs = [s for s in source if isinstance(s, dict)]
    elif isinstance(source, dict) and set(source) & _STEP_KEYS:
        specs = [source]
    elif isinstance(source, dict):
        # Rows or a configuration written inline: nothing here names a dataset.
        return Advice("source_as_dataset", f"{_SOURCE_IS_ONE} the backend manages. Rows written inline are not "
                      "a source; a query over the source computes them.")
    else:
        return None
    explanation = (
        f"{_SOURCE_IS_ONE}. A relation built inline, filtered or projected, is a "
        "dataset of its own: materialize it first, then use its name as the source, or as the right dataset of a join."
    )
    if not specs:
        return Advice("source_as_dataset", explanation)
    first = specs[0]
    ds_key = next((k for k in _DATASET_KEYS if isinstance(first.get(k), str)), None)
    if ds_key is None:
        if len(specs) == 1 and isinstance(source, dict):
            return Advice("source_as_dataset", f"{_SOURCE_IS_ONE}. The object given as the source is a transform: "
                          "name the dataset it reads as the source and send the object as the transform.")
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


def _transform_required(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """The call came without its transform: the step forms, and that an empty list is the identity."""
    if not (isinstance(err.details, dict) and err.details.get("missing") == "transform"):
        return None
    return Advice(
        "transform_required",
        "transform is required: the steps that read the source, as a list such as [{\"filter\": \"amount > 100\"}, "
        "{\"select\": [\"order_id\", \"amount\"]}] or one compact object such as {\"filter\": \"...\", \"group_by\": "
        "[...], \"measures\": [...]}; a raw_query first step holds SQL over input. An empty list previews the source as "
        "it is, or with materialize_result copies it under the new name.",
    )


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


_ALIAS_RE = re.compile(r"""^(?P<expr>.+?)\s+as\s+(?P<alias>"[^"]+"|'[^']+'|[^\s"'()]+)\s*$""", re.IGNORECASE)


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


_PLAIN_NAME = re.compile(r"[^\W\d]\w*")
_EXPRESSION_WORDS = {"and", "or", "not", "in", "is", "null", "true", "false", "cast", "as"}


def _name_in_expression(name: str) -> str:
    if _PLAIN_NAME.fullmatch(name) and name.lower() not in _EXPRESSION_WORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def _derived(step: Any) -> tuple[str, Any] | None:
    """A normalized derive step's name (as the backend names it) and expression."""
    if not isinstance(step, dict) or str(step.get("type") or "").lower() != "derive":
        return None
    body = step["derive"] if isinstance(step.get("derive"), dict) else step
    return (slugify(str(body["name"])), body.get("expression")) if body.get("name") else None


def _join_key_types(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """Join keys of different types: cast the left key to the right key's type in a derive, then join on it."""
    facts = err.details.get("join_key_types")
    if err.code != ErrorCode.INVALID_TRANSFORM or not isinstance(facts, dict):
        return None
    left, right, target = facts["left"], facts["right"], facts["right_type"]
    cast = f"cast({_name_in_expression(left)} as {target})"
    resolution = err.resolution or {}
    pairs, failed, steps, index = facts.get("pairs"), facts.get("failed"), resolution.get("steps"), resolution.get("index")
    located = (isinstance(steps, list) and isinstance(index, int) and 0 <= index < len(steps)
               and isinstance(pairs, list) and isinstance(failed, int)
               and all(isinstance(side, str) for pair in pairs for side in pair))
    # A later join on the same key reuses the column an earlier derive cast; a column of that name made any other way
    # stays, and the cast takes a name not in scope.
    available = set(facts.get("available") or [])
    base = slugify(f"{left}_as_{target}")
    reused = base in available and located and any(_derived(s) == (base, cast) for s in steps[:index])
    derived, suffix = base, 2
    while not reused and derived in available:
        derived, suffix = f"{base}_{suffix}", suffix + 1
    join_on = json.dumps({derived: right}, ensure_ascii=False)
    if reused:
        how = f"{derived}, derived earlier as {cast}, is already {target}: join on {join_on}."
    else:
        shown = json.dumps({"derive": {"name": derived, "expression": cast}}, ensure_ascii=False)
        how = f"Derive the left key as {target} first, {shown}, then join on {join_on}."
    explanation = (
        f"{left} is {facts['left_type']} and {right} in {facts['right_dataset']} is {target}; a join compares keys of "
        f"one type. {how} A value that does not convert fails the transform; when some keys may not convert (blanks, "
        f"other text), derive with try_cast({_name_in_expression(left)} as {target}) instead, which leaves them null and "
        "unmatched."
    )
    # The rewrite is the steps as the backend normalized them: a compact object is several steps there, so the cast
    # lands right before the refused join (after a raw_query, not before it), and an earlier join with the same on
    # is left alone.
    if not located:
        return Advice("join_key_types", explanation)
    steps = copy.deepcopy(steps)
    step = steps[index]
    holder = step if "on" in step else next(
        (step[k] for k in _JOIN_KEYS + _RIGHT_KEYS if isinstance(step.get(k), dict) and "on" in step[k]), None)
    if holder is None:
        return Advice("join_key_types", explanation)
    holder["on"] = {(derived if i == failed else pair[0]): pair[1] for i, pair in enumerate(pairs)}
    if not reused:
        steps.insert(index, {"type": "derive", "name": derived, "expression": cast})
    return Advice("join_key_types", explanation,
                  rewrite=[call(tool, **_call_arguments(tool, arguments, transform=steps))])


def _join_scope_names(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """After a join, names are unqualified and a right-side key is not copied: it equals the left key."""
    if err.code not in (ErrorCode.NOT_FOUND, ErrorCode.INVALID_TRANSFORM) or "join_key_types" in err.details:
        return None  # a refusal of the key types is about the keys, not the names after the join
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


_QUERY_SHAPE = ("raw_query takes one read-only SELECT (or WITH ... SELECT) statement in DuckDB SQL that reads only "
                "its placeholders: input, the transform's source, and each name bound under inputs, as "
                "{\"raw_query\": {\"sql\": \"SELECT ... FROM input JOIN customers USING (customer_id)\", "
                "\"inputs\": {\"customers\": \"customers\"}}}. It is the first step and semantic steps may follow it; "
                "whatever the semantic steps express needs no raw_query.")
_STATEMENT_ADVICE = {
    "CREATE": "To keep a query's result, send the SELECT as the raw_query of materialize_result with a name; the "
              "backend creates the dataset with its version, lineage and audit.",
    "DDL": "Datasets are removed with delete_dataset and never altered in place; a new shape is a new dataset, "
           "made with materialize_result.",
    "DML": "Datasets are never changed in place: select the rows you want and materialize them as a new dataset.",
    "COPY": "Files are read by import_dataset or import_workspace and written by export_result.",
    "ATTACH": "Another database is imported with import_dataset or import_workspace; a query sees only its placeholders.",
    "LOAD": "No extension can be loaded; a query has DuckDB's built-in functions.",
    "INSTALL": "No extension can be installed; a query has DuckDB's built-in functions.",
    "PRAGMA": "Settings belong to the server; a query can neither read nor change them.",
    "SET": "Settings belong to the server; a query can neither read nor change them.",
    "CALL": "Procedures are not available; write the rows you want as a SELECT.",
    "DESCRIBE": "describe_dataset shows a dataset's schema, types, sample and lineage.",
    "EXPLAIN": "explain=true on the transform returns the canonical IR, the plan and the SQL the backend runs.",
}


def _query_step(transform: Any) -> tuple[list[dict[str, Any]], int, dict[str, Any]] | None:
    """The transform's steps, and the index and body of the one that carries a raw_query."""
    steps = _steps(transform)
    for index, step in enumerate(steps):
        kind = _pick(step, *_TYPE_KEYS)
        if (isinstance(kind, str) and kind.lower() in _QUERY_KEYS) or any(k in step for k in _QUERY_KEYS):
            return steps, index, step
    return None


def _query_parts(step: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """The SQL and the named inputs of a raw_query step, in any of the forms it is written in."""
    body, inputs = step.get("raw_query"), step.get("inputs")
    if isinstance(body, dict):
        sql, inputs = body.get("sql"), body.get("inputs", inputs)
    else:
        sql = body if isinstance(body, str) else step.get("sql")
    if isinstance(inputs, list):
        inputs = {item: item for item in inputs if isinstance(item, str)}
    return (sql if isinstance(sql, str) else None), (dict(inputs) if isinstance(inputs, dict) else {})


def _query_restated(step: dict[str, Any], sql: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """The step with its raw_query written as {"raw_query": {"sql", "inputs"}}; a compact object's other keys stay."""
    rest = {k: v for k, v in step.items() if k not in ("raw_query", "sql", "inputs") + _TYPE_KEYS}
    body: dict[str, Any] = {"sql": sql}
    if inputs:
        body["inputs"] = inputs
    return {"raw_query": body, **rest}


def _raw_query(err: BackendError, tool: str, arguments: dict[str, Any]) -> Advice | None:
    """A refused or failed raw_query: the accepted shape, and the request rewritten when the repair is mechanical."""
    facts = err.details.get("raw_query") if isinstance(err.details, dict) else None
    if not isinstance(facts, dict):
        return None
    refused = str(facts.get("refused"))
    kind = f"raw_query_{refused}"
    placeholders = facts.get("placeholders") if isinstance(facts.get("placeholders"), dict) else {}
    listed = ", ".join(f"{p} ({d})" for p, d in placeholders.items()) or "input"
    found = _query_step(arguments.get("transform"))
    steps, index, step = found if found is not None else ([], -1, {})
    sql, inputs = _query_parts(step) if found is not None else (None, {})

    def resend(new_sql: str | None = None, new_inputs: dict[str, Any] | None = None, **overrides: Any) -> list[dict[str, Any]]:
        """The same call with the raw_query step's SQL or inputs replaced."""
        changed = list(steps)
        changed[index] = _query_restated(step, new_sql if new_sql is not None else sql or "",
                                         inputs if new_inputs is None else new_inputs)
        transform = _rebuild(arguments.get("transform"), changed)
        return [call(tool, **_call_arguments(tool, arguments, transform=transform, **overrides))]

    if refused == "empty":
        return Advice(kind, "The raw_query step carries no SQL. " + _QUERY_SHAPE)
    if refused == "unavailable":
        return Advice(kind, "This backend's engine cannot run raw_query; express the request with the semantic steps.")
    if refused == "parse":
        suggested = facts.get("suggested_sql")
        text = f"The SQL does not parse ({facts.get('message')}). "
        if isinstance(suggested, str) and found is not None:
            return Advice(kind, text + "A placeholder is a bare table name, not a $parameter; the same statement "
                          "without the $ is below. " + _QUERY_SHAPE, rewrite=resend(new_sql=suggested))
        return Advice(kind, text + _QUERY_SHAPE)
    if refused == "statement":
        category = "CREATE" if facts.get("statement") in ("CREATE", "CREATE_FUNC") else str(facts.get("category"))
        command = facts.get("command") or facts.get("statement")
        text = f"{command} is not a query; raw_query only reads. " + _STATEMENT_ADVICE.get(category, "") + " "
        create_as = facts.get("create_as")
        if isinstance(create_as, dict) and found is not None and isinstance(arguments.get("source"), str):
            changed = list(steps)
            changed[index] = _query_restated(step, str(create_as["sql"]), inputs)
            rewrite = [call("materialize_result", source=arguments.get("source"),
                            transform=_rebuild(arguments.get("transform"), changed),
                            name=slugify(str(create_as["name"])), description=arguments.get("description"))]
            return Advice(kind, text + "The same SELECT, materialized under that name, is below.", rewrite=rewrite)
        return Advice(kind, text + _QUERY_SHAPE)
    if refused == "statements":
        text = f"The SQL holds {len(facts.get('statements') or [])} statements; raw_query runs exactly one. "
        if str(facts.get("command")) in ("PIVOT", "UNPIVOT"):
            text += ("A PIVOT without an IN list first creates a type for its values, which is a second statement; "
                     "list the values instead: PIVOT input ON region IN ('East', 'West') USING sum(amount). ")
        else:
            text += "Combine them into one query with WITH ... SELECT, or send each as its own transform. "
        return Advice(kind, text + _QUERY_SHAPE)
    if refused == "parameter":
        suggested = facts.get("suggested_sql")
        text = ("raw_query takes no parameters ($name, $1 or ?): write each value into the SQL as a literal, such as "
                "WHERE region = 'West'. ")
        if isinstance(suggested, str) and found is not None:
            return Advice(kind, text + "A placeholder is a bare table name; the same statement without the $ is below.",
                          rewrite=resend(new_sql=suggested))
        return Advice(kind, text + _QUERY_SHAPE)
    if refused in ("file_function", "replacement_scan"):
        what = f"{facts.get('function')}()" if refused == "file_function" else f"FROM {facts.get('reference')!r}"
        return Advice(kind, f"{what} reads from outside the backend, and a raw_query reads only the datasets bound to "
                            f"its placeholders ({listed}). Import the file with import_dataset (a directory with "
                            "import_workspace), bind the new dataset under inputs and read it by that name. " + _QUERY_SHAPE)
    if refused == "table_function":
        return Advice(kind, f"{facts.get('function')}() is not available in a raw_query; the table functions it takes "
                            f"are {', '.join(facts.get('allowed') or [])}, and everything else it reads is a "
                            f"placeholder ({listed}).")
    if refused == "physical_table":
        return Advice(kind, f"Storage tables are not visible to a query; it reads datasets only through its "
                            f"placeholders ({listed}). Bind the dataset you mean under inputs, by its name, and read "
                            "it by that placeholder.")
    if refused == "qualified_table":
        return Advice(kind, f"{facts.get('reference')} names a schema or a database; a placeholder is a bare name "
                            f"({listed}), and any other dataset is bound under inputs to be read by name.")
    if refused == "cte_shadows_placeholder":
        return Advice(kind, "A WITH clause that takes a placeholder's name hides the dataset bound to it; give the "
                            "CTE its own name and read the placeholder inside it.")
    if refused == "unknown_placeholder":
        names = [str(n) for n in facts.get("names") or []]
        datasets = facts.get("datasets") if isinstance(facts.get("datasets"), dict) else {}
        text = f"The SQL reads {', '.join(names)}, but the placeholders bound here are {listed}."
        if names and all(n in datasets for n in names) and found is not None:
            bound = {**inputs, **{n: datasets[n] for n in names}}
            text += (" " + ", ".join(f"{n} is the dataset {datasets[n]}" for n in names)
                     + ": bound under inputs, as below, the query reads it by that name.")
            return Advice(kind, text, rewrite=resend(new_inputs=bound))
        return Advice(kind, text + " Bind each dataset under inputs by the name the SQL uses "
                                   "({\"inputs\": {\"customers\": \"customers\"}}), or read a placeholder instead.")
    if refused == "unused_input":
        names = [str(n) for n in facts.get("names") or []]
        text = (f"inputs binds {', '.join(names)}, which the SQL never reads; each bound dataset becomes an upstream "
                "of the result, so bind only what the query reads.")
        if found is not None and all(n in inputs for n in names):
            return Advice(kind, text, rewrite=resend(new_inputs={k: v for k, v in inputs.items() if k not in names}))
        return Advice(kind, text)
    if refused == "source_unused":
        used = [str(n) for n in facts.get("used") or []]
        text = (f"The SQL never reads input, the placeholder of the transform's source "
                f"({placeholders.get('input', 'source')}), which would still become an upstream of the result. Read "
                "it, or make a dataset the query does read the transform's source.")
        if used and found is not None and used[0] in inputs:
            return Advice(kind, text + f" The same request with {used[0]}'s dataset as the source is below.",
                          rewrite=resend(source=inputs[used[0]]))
        return Advice(kind, text)
    if refused == "input_not_found":
        available = [str(d.get("name")) for d in err.details.get("available") or [] if isinstance(d, dict)]
        text = (f"No dataset matches {facts.get('reference')!r}, the dataset bound to {facts.get('name')}; the datasets "
                f"here are {', '.join(available) if available else 'none'}. Bind one of them under inputs by its name "
                "or id, or a description that names it.")
        if err.details.get("restorable"):
            text += " The dataset was deleted; restore_dataset brings it back."
        return Advice(kind, text)
    if refused == "placeholder_name":
        return Advice(kind, "A placeholder is a table name in the SQL: it starts with a letter and holds letters, "
                            "digits and underscores, and input is kept for the transform's source. Name each input: "
                            "{\"inputs\": {\"customers\": \"<dataset reference>\"}}.")
    if refused == "not_first":
        text = ("raw_query must be the first step: input is the transform's source and each placeholder binds a "
                "dataset, not the result of the steps before it. Materialize the steps before it and run the query "
                "over that dataset, as below; semantic steps may follow the query.")
        source = arguments.get("source")
        if found is not None and index > 0 and isinstance(source, str):
            prepared = slugify(f"{source}_prepared")
            before = steps[:index]
            rewrite = [call("materialize_result", source=source, transform=before if len(before) > 1 else before[0],
                            name=prepared, description=f"{source}, prepared for a raw_query"),
                       call(tool, **_call_arguments(tool, arguments, source=prepared, transform=steps[index:]))]
            return Advice(kind, text, rewrite=rewrite)
        return Advice(kind, text)
    if refused == "binding":
        columns = facts.get("columns") if isinstance(facts.get("columns"), dict) else {}
        shown = "; ".join(f"{p}: {', '.join(str(c) for c in cols)}" for p, cols in columns.items())
        return Advice(kind, f"DuckDB could not bind the statement over its placeholders: {facts.get('message')}"
                            + (f" The columns of each placeholder are {shown}." if shown else ""))
    if refused == "output_type":
        return Advice(kind, "The backend holds integer, float, boolean, string, date and timestamp columns; cast each "
                            "other column in the SQL to one of them, e.g. CAST(tags AS VARCHAR), or leave it out.")
    if refused == "output_name":
        return Advice(kind, "Give each output column its own name with AS, e.g. count(*) AS orders; names are "
                            "compared without regard to case.")
    if refused == "deadline":
        seconds = facts.get("timeout_seconds")
        limit = f"after {seconds:g} seconds" if isinstance(seconds, (int, float)) else "when it runs too long"
        return Advice(kind, f"This server stops a raw_query {limit}. Make the statement cheaper (filter and aggregate "
                            "before joining, avoid cross joins) or split it: materialize an intermediate result and "
                            "query that. The semantic steps have no such deadline.")
    if refused == "execution":
        return Advice(kind, f"The statement was accepted and failed on the data ({facts.get('message')}). Fix the "
                            "expression that fails; try_cast(x AS type) gives NULL for a value that does not convert.")
    return Advice(kind, _QUERY_SHAPE)


Detector = Callable[[BackendError, str, dict[str, Any]], Advice | None]

# Tools whose refusals may name a document or a file where a dataset was expected.
_DATASET_TOOLS = _TRANSFORM_TOOLS + ("describe_dataset", "get_provenance", "publish_dataset", "update_metadata",
                                     "delete_dataset", "restore_dataset", "export_result")

# Every detector, the tools it applies to (None for every tool), and whether it is a fallback: a fallback
# runs only when no other detector recognised the refusal, so a general explanation never stacks on a
# specific one. Order is the order advice is listed in.
_REGISTRY: list[tuple[Detector, tuple[str, ...] | None, bool]] = [
    (_raw_query, _TRANSFORM_TOOLS, False),
    (_subquery, _TRANSFORM_TOOLS, False),
    (_limit_tail, _TRANSFORM_TOOLS, False),
    (_like, _TRANSFORM_TOOLS, False),
    (_sql_spelling, _TRANSFORM_TOOLS, False),
    (_sql_in_expression, _TRANSFORM_TOOLS, False),
    (_cast_conversion, _TRANSFORM_TOOLS, False),
    (_distinct, _TRANSFORM_TOOLS, False),
    (_join_on, _TRANSFORM_TOOLS, False),
    (_aggregate_in_select, _TRANSFORM_TOOLS, False),
    (_expression_in_select, _TRANSFORM_TOOLS, False),
    (_inline_source, _TRANSFORM_TOOLS, False),
    (_transform_required, _TRANSFORM_TOOLS, False),
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
    (_join_key_types, _TRANSFORM_TOOLS, False),
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
    distinct_keys: int | None = None,
) -> Advice | None:
    """A result that has, or mechanically reshapes to, the declared output shape: say so, with the next call.

    ``distinct_keys`` counts the result's distinct combinations of a one_per contract's keys. export_result
    checks rows with that count, so without it a keyed result is never said to match.
    """
    if contract.status != ContractStatus.OPEN:
        return None
    actual = [(f.name, f.logical_type) for f in ir.output_schema]
    problems = verify_columns(contract, actual) + verify_rows(contract, row_count, distinct_keys)
    keyed = contract.rows == RowCardinality.ONE_PER and all(k in {n for n, _ in actual} for k in contract.row_keys)
    if not problems and keyed and row_count > 0 and distinct_keys is None:
        return None
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
