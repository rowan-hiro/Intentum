"""Output contracts: the declared shape of a deliverable, and how it is checked.

The agent writes the contract down while the requirement is in front of it;
the backend keeps it and holds every export to it (MADR 0007). This module
owns the two halves that do not touch storage: reading a loose declaration
into a canonical one, and comparing a canonical contract with what a dataset
actually is. The trust model behind it (MADR 0008): a fresh declaration is
taken as given, and anything the agent later reproduces from memory is
checked against this record rather than believed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import InvalidIntentError
from ..ir.typing import comparable
from ..models.entities import ContractColumn, LogicalType, OutputContract, RowCardinality
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
    description: str

    def same_shape_as(self, contract: OutputContract) -> bool:
        return (
            [(c.name, c.logical_type) for c in self.columns] == [(c.name, c.logical_type) for c in contract.columns]
            and self.rows == contract.rows
            and self.row_keys == contract.row_keys
        )


@dataclass(frozen=True)
class ContractProblem:
    kind: str  # missing | renamed | extra | order | type | rows
    message: str
    column: str | None = None
    actual: str | None = None  # for "renamed": the dataset column that stands in for the declared one

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.column is not None:
            body["column"] = self.column
        if self.actual is not None:
            body["actual"] = self.actual
        return body


# --------------------------------------------------------------------------
# Reading a declaration
# --------------------------------------------------------------------------

def parse_contract(columns: Any, rows: Any = None, description: Any = None) -> ContractSpec:
    parsed = _parse_columns(columns)
    cardinality, keys = _parse_rows(rows, parsed)
    return ContractSpec(columns=parsed, rows=cardinality, row_keys=keys,
                        description=str(description).strip() if description else "")


def _parse_type(value: Any, column: str) -> LogicalType:
    key = str(value).strip().lower()
    if key in TYPE_ALIASES:
        return TYPE_ALIASES[key]
    raise InvalidIntentError(
        f"Unknown type {value!r} for column {column!r}.", field="columns",
        details={"allowed_types": sorted(set(str(t) for t in TYPE_ALIASES.values()))},
    )


def _parse_columns(loose: Any) -> list[ContractColumn]:
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
    for item in loose:
        if isinstance(item, str):
            name, logical_type = item, None
        elif isinstance(item, dict):
            unknown = sorted(k for k in item if k not in ("name", "column", "type", "logical_type"))
            if unknown:
                raise InvalidIntentError(f"Unknown key(s) {unknown} in a column declaration.", field="columns",
                                         details={"allowed_keys": ["name", "type"]})
            name = item.get("name", item.get("column"))
            raw_type = item.get("type", item.get("logical_type"))
            logical_type = _parse_type(raw_type, str(name)) if raw_type not in (None, "") else None
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
    return columns


def _parse_rows(loose: Any, columns: list[ContractColumn]) -> tuple[RowCardinality | None, list[str]]:
    if loose is None or loose == "" or loose == {}:
        return None, []
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
        declared = {c.name for c in columns}
        by_norm = {normalize(c.name): c.name for c in columns}
        resolved: list[str] = []
        for key in keys:
            key = key.strip()
            if key in declared:
                resolved.append(key)
            elif normalize(key) in by_norm:
                resolved.append(by_norm[normalize(key)])
            else:
                raise InvalidIntentError(f"one_per key {key!r} is not one of the declared columns.", field="rows",
                                         candidates=sorted(declared))
        if len(set(resolved)) != len(resolved):
            raise InvalidIntentError("one_per lists the same key twice.", field="rows")
        return RowCardinality.ONE_PER, resolved
    raise InvalidIntentError(f"Unsupported rows declaration {loose!r}.", field="rows")


# --------------------------------------------------------------------------
# Checking a dataset against a contract
# --------------------------------------------------------------------------

def verify_columns(contract: OutputContract, actual: list[tuple[str, LogicalType]]) -> list[ContractProblem]:
    """Compare declared columns (name, order, type) with a dataset's columns.

    Names must match exactly; a column that matches only after normalization
    is reported as ``renamed`` so the repair can carry the rename. Declared
    types are compared by family (numeric with numeric, temporal with
    temporal), because a contract says what kind of value a column holds,
    not how the engine stores it.
    """
    problems: list[ContractProblem] = []
    actual_names = [name for name, _ in actual]
    actual_types = dict(actual)
    by_norm: dict[str, list[str]] = {}
    for name in actual_names:
        by_norm.setdefault(normalize(name), []).append(name)
    matched: list[str | None] = []
    for declared in contract.columns:
        if declared.name in actual_types:
            found: str | None = declared.name
        else:
            near = by_norm.get(normalize(declared.name), [])
            found = near[0] if len(near) == 1 else None
            if found is not None:
                problems.append(ContractProblem("renamed", f"declared {declared.name!r} exists as {found!r}.", declared.name, found))
            else:
                problems.append(ContractProblem("missing", f"declared column {declared.name!r} is not in the dataset.", declared.name))
        matched.append(found)
        if found is not None and declared.logical_type is not None:
            if not comparable(declared.logical_type, actual_types[found]):
                problems.append(ContractProblem(
                    "type", f"{declared.name!r} was declared {declared.logical_type} but is {actual_types[found]}.", declared.name))
    used = {m for m in matched if m is not None}
    for name in actual_names:
        if name not in used:
            problems.append(ContractProblem("extra", f"column {name!r} is not in the contract.", name))
    present = [m for m in matched if m is not None]
    if not any(p.kind in ("missing", "extra") for p in problems) and present != actual_names:
        problems.append(ContractProblem("order", f"columns are ordered {actual_names}, the contract says {[c.name for c in contract.columns]}."))
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

    Only column problems can be repaired mechanically (rename near-misses,
    then select the declared columns in order); a missing column or a row
    problem needs the agent to go back to the data.
    """
    if not problems or any(p.kind in ("missing", "type", "rows") for p in problems):
        return None
    renames = {p.actual: p.column for p in problems if p.kind == "renamed" and p.actual and p.column}
    transform: dict[str, Any] = {}
    if renames:
        transform["rename"] = renames
    transform["select"] = [c.name for c in contract.columns]
    return transform


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
    if contract.description:
        body["description"] = contract.description
    if contract.satisfied_by:
        body["satisfied_by"] = contract.satisfied_by
        body["dataset_id"] = contract.dataset_id
        body["dataset_version"] = contract.dataset_version
    return body
