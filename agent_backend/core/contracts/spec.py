"""Output contracts: the declared shape of a deliverable, and how it is checked.

The agent writes the contract down while the requirement is in front of it;
the backend keeps it and holds every export to it (MADR 0007). This module
owns the two halves that do not touch storage: reading a loose declaration
into a canonical one, and comparing a canonical contract with what a dataset
actually is.

A contract names two kinds of column (MADR 0012). The ones in ``columns`` are
what the answer *carries*, in order. The ones in ``order_by`` and in the
``one_per`` keys are what it is *organized by*: a question that says "in
treatment id order" or "per day" names a column in an adverbial role, and that
column does not have to be part of the answer. Organizing columns are expected
in the dataset at export, so the backend can order and count by them, and are
left out of the file. The trust model behind it (MADR 0008): a fresh declaration is
taken as given, and anything the agent later reproduces from memory is
checked against this record rather than believed.

A contract may also hold the values of the columns it names to checks
(MADR 0020): ``not_null``, and an inclusive range whose bounds are numbers or
ISO dates and timestamps. They are what the agent wrote down; the backend
counts the values that violate them and never guesses what a value should be.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import InvalidIntentError
from ..ir.typing import comparable
from ..models.entities import (ContractCheck, ContractColumn, ContractOrder, LogicalType, OutputContract,
                               RowCardinality)
from ..naming import normalize

TYPE_ALIASES: dict[str, LogicalType] = {
    "int": LogicalType.INTEGER, "integer": LogicalType.INTEGER, "bigint": LogicalType.INTEGER,
    "float": LogicalType.FLOAT, "double": LogicalType.FLOAT, "real": LogicalType.FLOAT,
    "decimal": LogicalType.FLOAT, "numeric": LogicalType.FLOAT, "number": LogicalType.FLOAT,
    "bool": LogicalType.BOOLEAN, "boolean": LogicalType.BOOLEAN,
    "str": LogicalType.STRING, "string": LogicalType.STRING, "text": LogicalType.STRING, "varchar": LogicalType.STRING,
    "date": LogicalType.DATE,
    "timestamp": LogicalType.TIMESTAMP, "datetime": LogicalType.TIMESTAMP,
}
_ROW_WORDS: dict[str, RowCardinality] = {
    "one": RowCardinality.ONE, "single": RowCardinality.ONE, "1": RowCardinality.ONE, "exactly_one": RowCardinality.ONE,
    "at_least_one": RowCardinality.AT_LEAST_ONE, "some": RowCardinality.AT_LEAST_ONE, "any": RowCardinality.AT_LEAST_ONE,
    ">=1": RowCardinality.AT_LEAST_ONE, "non_empty": RowCardinality.AT_LEAST_ONE, "nonempty": RowCardinality.AT_LEAST_ONE,
}


@dataclass(frozen=True)
class ContractSpec:
    """A declaration read into canonical form, before it is stored."""

    columns: list[ContractColumn]
    rows: RowCardinality | None
    row_keys: list[str]
    order_by: list[ContractOrder]
    description: str
    checks: list[ContractCheck] = field(default_factory=list)

    def same_shape_as(self, contract: OutputContract) -> bool:
        """Whether this declaration says what the contract says: shape and value checks alike."""
        return (
            [(c.name, c.logical_type) for c in self.columns] == [(c.name, c.logical_type) for c in contract.columns]
            and self.rows == contract.rows
            and self.row_keys == contract.row_keys
            and [(o.name, o.descending) for o in self.order_by] == [(o.name, o.descending) for o in contract.order_by]
            and self.checks == contract.checks
        )


@dataclass(frozen=True)
class ContractProblem:
    kind: str  # missing | extra | order | type | rows | check
    message: str
    column: str | None = None
    evidence: dict[str, Any] | None = None  # for a failed value check: the check, the counts and offending rows

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.column is not None:
            body["column"] = self.column
        if self.evidence is not None:
            body["evidence"] = self.evidence
        return body


# --------------------------------------------------------------------------
# Reading a declaration
# --------------------------------------------------------------------------

def parse_contract(columns: Any, rows: Any = None, order_by: Any = None, description: Any = None,
                   checks: Any = None) -> ContractSpec:
    parsed, inline = _parse_columns(columns)
    cardinality, keys = _parse_rows(rows, parsed)
    order = _parse_order(order_by, parsed)
    checked = _parse_checks(checks, inline, parsed, keys, order)
    return ContractSpec(columns=parsed, rows=cardinality, row_keys=keys, order_by=order,
                        description=str(description).strip() if description else "", checks=checked)


def organizing_keys(contract: OutputContract | ContractSpec) -> list[str]:
    """The columns the deliverable is organized by but does not carry.

    The export needs them in the dataset — it orders and counts by them — and
    leaves them out of the file. A column that is both ordered by and carried
    is not one of these: it is simply carried.
    """
    carried = {normalize(c.name) for c in contract.columns}
    keys: list[str] = []
    seen: set[str] = set()
    for name in [o.name for o in contract.order_by] + list(contract.row_keys):
        key = normalize(name)
        if key in carried or key in seen:
            continue
        seen.add(key)
        keys.append(name)
    return keys


def _parse_type(value: Any, column: str) -> LogicalType:
    key = str(value).strip().lower()
    if key in TYPE_ALIASES:
        return TYPE_ALIASES[key]
    raise InvalidIntentError(
        f"Unknown type {value!r} for column {column!r}.", field="columns",
        details={"allowed_types": sorted(set(str(t) for t in TYPE_ALIASES.values()))},
    )


_CHECK_KEYS = ("not_null", "min", "max", "range")


def _parse_columns(loose: Any) -> tuple[list[ContractColumn], list[dict[str, Any]]]:
    """The declared columns, and the checks written inside a column declaration ({"name", "type", "min", ...})."""
    if isinstance(loose, str):
        loose = [loose]
    if isinstance(loose, dict):
        # {"name": "type", ...} — a compact declaration with types
        loose = [{"name": k, "type": v} for k, v in loose.items()]
    if not isinstance(loose, list) or not loose:
        raise InvalidIntentError(
            "columns must be a non-empty list naming the columns the output will carry, in order.",
            field="columns",
            hint='Example: ["treatmentname"], or [{"name": "day", "type": "date"}, {"name": "total", "type": "float"}].',
        )
    columns: list[ContractColumn] = []
    inline: list[dict[str, Any]] = []
    for item in loose:
        if isinstance(item, str):
            name, logical_type = item, None
        elif isinstance(item, dict):
            unknown = sorted(k for k in item if k not in ("name", "column", "type", "logical_type") + _CHECK_KEYS)
            if unknown:
                raise InvalidIntentError(f"Unknown key(s) {unknown} in a column declaration.", field="columns",
                                         details={"allowed_keys": ["name", "type", *_CHECK_KEYS]})
            name = item.get("name", item.get("column"))
            raw_type = item.get("type", item.get("logical_type"))
            logical_type = _parse_type(raw_type, str(name)) if raw_type not in (None, "") else None
            written = {k: item[k] for k in _CHECK_KEYS if k in item}
            if written and isinstance(name, str):
                inline.append({"column": name.strip(), **written})
        else:
            raise InvalidIntentError(f"Invalid column declaration {item!r}.", field="columns")
        if not isinstance(name, str) or not name.strip():
            raise InvalidIntentError(f"Column declaration {item!r} has no name.", field="columns")
        columns.append(ContractColumn(name=name.strip(), logical_type=logical_type))
    seen: dict[str, str] = {}
    for column in columns:
        key = normalize(column.name)
        if key in seen:
            raise InvalidIntentError(
                f"Column {column.name!r} is declared twice" + ("" if seen[key] == column.name else f" (as {seen[key]!r} too)") + ".",
                field="columns", candidates=[c.name for c in columns],
            )
        seen[key] = column.name
    return columns, inline


ROWS_ALLOWED: list[Any] = ["one", "at_least_one", {"one_per": ["column", "..."]}]
_ROWS_HINT = ('A single value is rows="one"; one row for every day is rows={"one_per": ["day"]}; '
              'an unknown number of rows is rows="at_least_one". A one_per key does not have to be '
              "a column the answer carries.")


def _parse_rows(loose: Any, columns: list[ContractColumn]) -> tuple[RowCardinality | None, list[str]]:
    if loose is None or loose == "" or loose == {} or loose == []:
        # Required: how many rows the answer has, and one row per what, is part
        # of reading the requirement, and the backend can check it for free.
        raise InvalidIntentError(
            "rows is required: say how many rows the answer has.", field="rows",
            hint=_ROWS_HINT, details={"allowed": ROWS_ALLOWED},
        )
    if isinstance(loose, bool):
        raise InvalidIntentError("rows must be 'one', 'at_least_one' or {\"one_per\": [columns]}.", field="rows")
    if isinstance(loose, int):
        if loose == 1:
            return RowCardinality.ONE, []
        raise InvalidIntentError(f"rows={loose} is not a cardinality the contract can hold; only 1 is.", field="rows",
                                 hint="Use 'one', 'at_least_one' or {\"one_per\": [columns]}.")
    if isinstance(loose, str):
        word = loose.strip().lower().replace(" ", "_").replace("-", "_")
        if word in _ROW_WORDS:
            return _ROW_WORDS[word], []
        if word.startswith("one_per_"):
            loose = {"one_per": [loose.strip()[8:]]}
        elif word == "one_per":
            raise InvalidIntentError("one_per needs the key columns: {\"one_per\": [\"column\"]}.", field="rows")
        else:
            raise InvalidIntentError(f"Unknown row cardinality {loose!r}.", field="rows",
                                     details={"allowed": ["one", "at_least_one", {"one_per": ["column", "..."]}]})
    if isinstance(loose, dict):
        unknown = sorted(k for k in loose if k not in ("one_per", "per", "key", "keys"))
        if unknown:
            raise InvalidIntentError(f"Unknown key(s) {unknown} in rows.", field="rows",
                                     details={"allowed_keys": ["one_per"]})
        raw_keys = next((loose[k] for k in ("one_per", "per", "key", "keys") if k in loose), None)
        keys = [raw_keys] if isinstance(raw_keys, str) else list(raw_keys or [])
        if not keys or not all(isinstance(k, str) and k.strip() for k in keys):
            raise InvalidIntentError("one_per needs at least one key column name.", field="rows")
        # A key names the grain, not the payload: "the daily maximum" is one row
        # per day whether or not the answer carries the day. A key that is also
        # a declared column keeps that column's spelling.
        by_norm = {normalize(c.name): c.name for c in columns}
        resolved: list[str] = []
        for key in keys:
            key = key.strip()
            resolved.append(by_norm.get(normalize(key), key))
        if len({normalize(k) for k in resolved}) != len(resolved):
            raise InvalidIntentError("one_per lists the same key twice.", field="rows")
        return RowCardinality.ONE_PER, resolved
    raise InvalidIntentError(f"Unsupported rows declaration {loose!r}.", field="rows")


_DIRECTIONS: dict[str, bool] = {"asc": False, "ascending": False, "up": False, "increasing": False,
                                "desc": True, "descending": True, "down": True, "decreasing": True}


def _parse_order(loose: Any, columns: list[ContractColumn]) -> list[ContractOrder]:
    """Read the columns the answer is sorted by; they need not be carried."""
    if loose is None or loose == "" or loose == [] or loose == {}:
        return []
    if isinstance(loose, (str, dict)):
        loose = [loose]
    if not isinstance(loose, list):
        raise InvalidIntentError(
            "order_by must name the column(s) the answer is sorted by.", field="order_by",
            hint='Example: ["treatmentid"], or [{"column": "day", "direction": "desc"}]. Prefix a name with '
                 '"-" for descending. A sort column does not have to be one the answer carries.',
        )
    by_norm = {normalize(c.name): c.name for c in columns}
    parsed: list[ContractOrder] = []
    for item in loose:
        if isinstance(item, str):
            name, descending = _parse_order_text(item)
        elif isinstance(item, dict):
            name, descending = _parse_order_object(item)
        else:
            raise InvalidIntentError(f"Invalid order_by entry {item!r}.", field="order_by")
        if not isinstance(name, str) or not name.strip():
            raise InvalidIntentError(f"order_by entry {item!r} names no column.", field="order_by")
        parsed.append(ContractOrder(name=by_norm.get(normalize(name.strip()), name.strip()), descending=descending))
    seen: set[str] = set()
    for order in parsed:
        key = normalize(order.name)
        if key in seen:
            raise InvalidIntentError(f"order_by names {order.name!r} twice.", field="order_by")
        seen.add(key)
    return parsed


def _parse_order_text(item: str) -> tuple[str, bool]:
    text = item.strip()
    if text.startswith("-"):
        return text[1:].strip(), True
    if text.startswith("+"):
        return text[1:].strip(), False
    lowered = text.lower()
    for word, descending in _DIRECTIONS.items():
        if lowered.endswith(" " + word):
            return text[: -(len(word) + 1)].strip(), descending
    return text, False


def _parse_order_object(item: dict[str, Any]) -> tuple[Any, bool]:
    unknown = sorted(k for k in item if k not in ("name", "column", "field", "direction", "order", "descending", "desc"))
    if unknown:
        raise InvalidIntentError(f"Unknown key(s) {unknown} in an order_by entry.", field="order_by",
                                 details={"allowed_keys": ["column", "direction"]})
    name = next((item[k] for k in ("column", "name", "field") if k in item), None)
    raw = next((item[k] for k in ("direction", "order") if k in item), None)
    if raw is not None:
        word = str(raw).strip().lower()
        if word not in _DIRECTIONS:
            raise InvalidIntentError(f"Unknown sort direction {raw!r}.", field="order_by",
                                     details={"allowed": ["asc", "desc"]})
        return name, _DIRECTIONS[word]
    flag = next((item[k] for k in ("descending", "desc") if k in item), None)
    return name, bool(flag)


CHECKS_HINT = ('checks hold the values of columns the contract names: [{"column": "rate", "not_null": true, '
               '"min": 0, "max": 100}], or {"rate": {"min": 0, "max": 100}}, or the same keys inside a column '
               'declaration. not_null is true or false; min and max are inclusive bounds, numbers for numeric '
               'columns or ISO dates and timestamps ("2025-01-01") for temporal ones; range: [min, max] is the '
               'same. A null value is not outside a range: add not_null to forbid it.')
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?")


def bound_kind(value: Any) -> str | None:
    """What a check bound is: number, date or timestamp; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return "number" if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        try:
            if _ISO_DATE.fullmatch(text):
                dt.date.fromisoformat(text)
                return "date"
            if _ISO_TIMESTAMP.fullmatch(text):
                dt.datetime.fromisoformat(text)
                return "timestamp"
        except ValueError:
            return None
    return None


def _ordered(value: Any) -> Any:
    return dt.datetime.fromisoformat(value.strip()) if isinstance(value, str) else value


def _check_items(loose: Any) -> list[Any]:
    if loose is None or loose == [] or loose == {} or loose == "":
        return []
    if isinstance(loose, dict):
        if any(k in loose for k in ("column", "name")):
            return [loose]
        items: list[Any] = []
        for column, body in loose.items():
            if not isinstance(body, dict):
                raise InvalidIntentError(f"The check on {column!r} must be an object such as {{\"min\": 0}}.",
                                         field="checks", hint=CHECKS_HINT)
            items.append({"column": column, **body})
        return items
    if isinstance(loose, list):
        return loose
    raise InvalidIntentError("checks must be a list of checks or an object keyed by column.", field="checks",
                             hint=CHECKS_HINT)


def _parse_checks(loose: Any, inline: list[dict[str, Any]], columns: list[ContractColumn], row_keys: list[str],
                  order_by: list[ContractOrder]) -> list[ContractCheck]:
    """Read value checks into canonical form, on the names the contract uses and in their declared spelling."""
    named = {normalize(n): n for n in [c.name for c in columns] + [o.name for o in order_by] + list(row_keys)}
    types = {normalize(c.name): c.logical_type for c in columns}
    checks: list[ContractCheck] = []
    seen: set[str] = set()
    for item in inline + _check_items(loose):
        if not isinstance(item, dict):
            raise InvalidIntentError(f"Invalid check {item!r}.", field="checks", hint=CHECKS_HINT)
        unknown = sorted(k for k in item if k not in ("column", "name") + _CHECK_KEYS)
        if unknown:
            raise InvalidIntentError(f"Unknown key(s) {unknown} in a check.", field="checks", hint=CHECKS_HINT,
                                     details={"allowed_keys": ["column", *_CHECK_KEYS]})
        raw = item.get("column", item.get("name"))
        if not isinstance(raw, str) or not raw.strip():
            raise InvalidIntentError(f"Check {item!r} names no column.", field="checks", hint=CHECKS_HINT)
        key = normalize(raw.strip())
        if key not in named:
            raise InvalidIntentError(
                f"A check names {raw.strip()!r}, which the contract does not name; a check holds a carried or "
                "organizing column of this contract.", field="checks", hint=CHECKS_HINT,
                details={"contract_names": list(named.values())})
        column = named[key]
        if key in seen:
            raise InvalidIntentError(f"{column!r} is checked twice; give one check per column.", field="checks",
                                     hint=CHECKS_HINT)
        seen.add(key)
        not_null = item.get("not_null", False)
        if not isinstance(not_null, bool):
            raise InvalidIntentError(f"not_null for {column!r} must be true or false.", field="checks",
                                     hint=CHECKS_HINT)
        low, high = item.get("min"), item.get("max")
        if "range" in item:
            span = item["range"]
            if low is not None or high is not None:
                raise InvalidIntentError(f"The check on {column!r} gives range beside min or max; give one of them.",
                                         field="checks", hint=CHECKS_HINT)
            if not isinstance(span, list) or len(span) != 2:
                raise InvalidIntentError(f"range for {column!r} must be [min, max].", field="checks",
                                         hint=CHECKS_HINT)
            low, high = span
        kinds = {bound: bound_kind(bound) for bound in (low, high) if bound is not None}
        wrong = [bound for bound, kind in kinds.items() if kind is None]
        if wrong:
            raise InvalidIntentError(f"The bound {wrong[0]!r} for {column!r} is neither a number nor an ISO date "
                                     "or timestamp.", field="checks", hint=CHECKS_HINT)
        families = {"number" if kind == "number" else "temporal" for kind in kinds.values()}
        if len(families) > 1:
            raise InvalidIntentError(f"The bounds for {column!r} mix a number with a date or timestamp.",
                                     field="checks", hint=CHECKS_HINT)
        if low is not None and high is not None and _ordered(low) > _ordered(high):
            raise InvalidIntentError(f"The check on {column!r} has min {low!r} above max {high!r}.", field="checks",
                                     hint=CHECKS_HINT)
        if not not_null and low is None and high is None:
            raise InvalidIntentError(f"The check on {column!r} checks nothing: give not_null, min or max.",
                                     field="checks", hint=CHECKS_HINT)
        declared = types.get(key)
        if families and declared is not None:
            fits = declared.is_numeric if families == {"number"} else declared.is_temporal
            if not fits:
                raise InvalidIntentError(
                    f"{column!r} is declared {declared}; a range with {' and '.join(sorted(set(kinds.values())))} "
                    "bounds holds a " + ("numeric" if families == {"number"} else "date or timestamp") + " column.",
                    field="checks", hint=CHECKS_HINT)
        checks.append(ContractCheck(column=column, not_null=not_null, min=low, max=high))
    return checks


def describe_check(check: ContractCheck) -> str:
    """The check as a sentence fragment: "rate is never null and lies in [0, 100]"."""
    return f"{check.column} {_condition(check)}"


def _condition(check: ContractCheck) -> str:
    parts: list[str] = []
    if check.not_null:
        parts.append("is never null")
    if check.min is not None and check.max is not None:
        parts.append(f"lies in [{check.min}, {check.max}]")
    elif check.min is not None:
        parts.append(f"is at least {check.min}")
    elif check.max is not None:
        parts.append(f"is at most {check.max}")
    return " and ".join(parts)


def check_summary(check: ContractCheck) -> dict[str, Any]:
    body: dict[str, Any] = {"column": check.column}
    if check.not_null:
        body["not_null"] = True
    if check.min is not None:
        body["min"] = check.min
    if check.max is not None:
        body["max"] = check.max
    return body


@dataclass(frozen=True)
class BoundCheck:
    """A contract check bound to the dataset column it stands for, with what the engine needs to count it."""

    check: ContractCheck
    column: str
    logical_type: LogicalType

    @property
    def cast(self) -> str | None:
        """The type a bound is cast to before it is compared: none for numbers."""
        if self.check.min is None and self.check.max is None or self.logical_type.is_numeric:
            return None
        return "DATE" if self.logical_type == LogicalType.DATE else "TIMESTAMP"

    @property
    def nan(self) -> bool:
        return self.logical_type == LogicalType.FLOAT


def bind_checks(contract: OutputContract | ContractSpec,
                actual: list[tuple[str, LogicalType]]) -> tuple[list[BoundCheck], list[ContractProblem]]:
    """The contract's checks on the dataset columns their names stand for, and the checks that cannot hold there.

    A check whose column is not in the dataset is left out: ``verify_columns`` reports the column as missing.
    A range holds a numeric column with number bounds or a date or timestamp column with temporal bounds; any
    other pairing is a problem, since the values could never be compared with the bounds.
    """
    types = dict(actual)
    stands = match_columns(contract, [name for name, _ in actual])
    bound: list[BoundCheck] = []
    problems: list[ContractProblem] = []
    for check in contract.checks:
        found = stands.get(check.column)
        if found is None:
            continue
        logical = types[found]
        kinds = {bound_kind(b) for b in (check.min, check.max) if b is not None}
        if kinds:
            fits = logical.is_numeric if kinds == {"number"} else logical.is_temporal
            if not fits:
                problems.append(ContractProblem(
                    "check", f"{check.column!r} is {logical}; the check says it {_condition(check)}, "
                             "and a range holds only a numeric column (number bounds) or a date or timestamp column "
                             "(ISO bounds).", check.column,
                    evidence={"check": check_summary(check), "type": str(logical)}))
                continue
        bound.append(BoundCheck(check, found, logical))
    return bound, problems


def check_problem(target: BoundCheck, nulls: int, outside: int, low: Any = None, high: Any = None,
                  rows: list[dict[str, Any]] | None = None) -> ContractProblem | None:
    """The problem a check's counts amount to, with the evidence; None when the values pass."""
    failed_nulls = nulls if target.check.not_null else 0
    if not failed_nulls and not outside:
        return None
    what: list[str] = []
    if failed_nulls:
        what.append(f"{failed_nulls} {'value is' if failed_nulls == 1 else 'values are'} null")
    if outside:
        what.append(f"{outside} {'value falls' if outside == 1 else 'values fall'} outside the range")
    evidence: dict[str, Any] = {"check": check_summary(target.check), "dataset_column": target.column,
                                "null_values": nulls, "values_outside": outside, "observed_min": low,
                                "observed_max": high}
    if rows is not None:
        evidence["rows"] = rows
    observed = f" (observed {low} to {high})" if low is not None or high is not None else ""
    return ContractProblem("check", f"the contract says {describe_check(target.check)}; "
                                    f"{' and '.join(what)}{observed}.",
                           target.check.column, evidence=evidence)


# --------------------------------------------------------------------------
# Checking a dataset against a contract
# --------------------------------------------------------------------------

def match_columns(contract: OutputContract | ContractSpec, actual: list[str]) -> dict[str, str]:
    """The dataset column each declared name stands for (MADR 0018).

    Every name the contract uses, carried or organizing, stands for the column
    of that exact name or, failing that, the one column whose normalized name
    is the same: a declared FirstProduct stands for firstproduct, which the
    transform language writes for it. A name that fits no column, or several
    only after normalization, is left out.
    """
    exact = set(actual)
    by_norm: dict[str, list[str]] = {}
    for name in actual:
        by_norm.setdefault(normalize(name), []).append(name)
    found: dict[str, str] = {}
    for declared in [c.name for c in contract.columns] + [o.name for o in contract.order_by] + list(contract.row_keys):
        if declared in found:
            continue
        near = [declared] if declared in exact else by_norm.get(normalize(declared), [])
        if len(near) == 1:
            found[declared] = near[0]
    return found


def respelled(contract: OutputContract | ContractSpec, actual: list[str]) -> dict[str, str]:
    """The carried columns the file writes under the contract's spelling: dataset name -> header."""
    stands = match_columns(contract, actual)
    return {stands[c.name]: c.name for c in contract.columns if c.name in stands and stands[c.name] != c.name}


def _unmatched(name: str, actual: list[str]) -> str:
    near = [a for a in actual if normalize(a) == normalize(name)]
    if len(near) > 1:
        return f"{name!r} fits {near} only after normalization; name one of them exactly."
    return f"{name!r} is not in the dataset."


def verify_columns(contract: OutputContract, actual: list[tuple[str, LogicalType]]) -> list[ContractProblem]:
    """Compare declared columns (name, order, type) with a dataset's columns.

    A declared name is matched as ``match_columns`` does: exactly, or by its
    normalized form when that fits one column, whose values the export then
    writes under the declared spelling (MADR 0018). Declared types are
    compared by family (numeric with numeric, temporal with temporal),
    because a contract says what kind of value a column holds, not how the
    engine stores it.
    """
    problems: list[ContractProblem] = []
    actual_names = [name for name, _ in actual]
    actual_types = dict(actual)
    stands = match_columns(contract, actual_names)
    matched: list[str | None] = []
    for declared in contract.columns:
        found = stands.get(declared.name)
        if found is None:
            problems.append(ContractProblem("missing", "declared column " + _unmatched(declared.name, actual_names),
                                            declared.name))
        matched.append(found)
        if found is not None and declared.logical_type is not None:
            if not comparable(declared.logical_type, actual_types[found]):
                problems.append(ContractProblem(
                    "type", f"{declared.name!r} was declared {declared.logical_type} but is {actual_types[found]}.", declared.name))
    used = {m for m in matched if m is not None}
    # Organizing columns are expected in the dataset and left out of the file:
    # the export orders and counts by them (MADR 0012).
    for key in organizing_keys(contract):
        found = stands.get(key)
        if found is None:
            problems.append(ContractProblem(
                "missing", "the contract is organized by " + _unmatched(key, actual_names).rstrip(".")
                           + "; the export needs it to order and count by, and does not write it to the file.", key))
        else:
            used.add(found)
    for name in actual_names:
        if name not in used:
            problems.append(ContractProblem("extra", f"column {name!r} is not in the contract.", name))
    present = [m for m in matched if m is not None]
    carried = [name for name in actual_names if name in set(present)]
    if not any(p.kind in ("missing", "extra") for p in problems) and present != carried:
        problems.append(ContractProblem("order", f"the carried columns are ordered {carried}, the contract says {[c.name for c in contract.columns]}."))
    return problems


def verify_rows(contract: OutputContract, row_count: int, distinct_keys: int | None) -> list[ContractProblem]:
    if contract.rows is None:
        return []
    if contract.rows == RowCardinality.ONE and row_count != 1:
        return [ContractProblem("rows", f"the contract says exactly one row; the dataset has {row_count}.")]
    if contract.rows == RowCardinality.AT_LEAST_ONE and row_count < 1:
        return [ContractProblem("rows", "the contract says at least one row; the dataset is empty.")]
    if contract.rows == RowCardinality.ONE_PER:
        if row_count < 1:
            return [ContractProblem("rows", f"the contract says one row per {contract.row_keys}; the dataset is empty.")]
        if distinct_keys is not None and distinct_keys != row_count:
            return [ContractProblem(
                "rows", f"the contract says one row per {contract.row_keys}; the dataset has {row_count} rows "
                        f"over {distinct_keys} distinct key(s).")]
    return []


def repair_transform(contract: OutputContract, problems: list[ContractProblem]) -> dict[str, Any] | None:
    """The transform that would give the dataset the declared shape, when one exists.

    Only column problems can be repaired mechanically (select the declared
    columns in order, keeping the organizing columns the export needs); a
    missing column, a row problem or a failed value check needs the agent to
    go back to the data: the backend never changes a value to pass a check. A
    name spelled differently from its column is not a problem to repair: the
    export writes the declared spelling (MADR 0018).
    """
    if not problems or any(p.kind in ("missing", "type", "rows", "check") for p in problems):
        return None
    return {"select": [c.name for c in contract.columns] + organizing_keys(contract)}


def contract_summary(contract: OutputContract) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": contract.id,
        "status": str(contract.status),
        "columns": [{"name": c.name, **({"type": str(c.logical_type)} if c.logical_type else {})} for c in contract.columns],
        "revision": contract.revision,
        "declared_by": contract.operation_id,
    }
    if contract.rows is not None:
        body["rows"] = {"one_per": contract.row_keys} if contract.rows == RowCardinality.ONE_PER else str(contract.rows)
    if contract.order_by:
        body["order_by"] = [{"column": o.name, "direction": "desc" if o.descending else "asc"} for o in contract.order_by]
    keys = organizing_keys(contract)
    if keys:
        body["organizing_columns"] = keys
    if contract.checks:
        body["checks"] = [check_summary(c) for c in contract.checks]
    if contract.description:
        body["description"] = contract.description
    if contract.satisfied_by:
        body["satisfied_by"] = contract.satisfied_by
        body["dataset_id"] = contract.dataset_id
        body["dataset_version"] = contract.dataset_version
    return body
