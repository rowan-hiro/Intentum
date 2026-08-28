"""How typed values become text in an exported file.

Rendering is a property of the file handed to another system, not of the data
(MADR 0005): the managed dataset keeps its types, and a specification decides
how each column is written. The specification is strict like the IR — unknown
keys are refused — so that the same dataset version plus the same
specification always yields the same bytes.

A specification has file-level defaults and per-column overrides::

    {"decimals": 4, "strip_trailing_zeros": true, "integer_min_decimals": 1,
     "columns": {"end_date": {"date_format": "%Y-%m-%d"}}}

Numbers are rounded half-up on the shortest decimal representation of the
value, so 31783696.815 rounds to 31783696.82 rather than to the .81 that the
binary double would give.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from ..errors import InvalidSchemaError
from ..naming import normalize

# strftime directives an export pattern may use. Locale-dependent ones (%c, %x,
# %X) are left out on purpose: an export must not depend on the host's locale.
ALLOWED_DIRECTIVES = set("YymdHMSfjBbAapZzGuUWV%")
_DIRECTIVE_RE = re.compile(r"%(.)")
MAX_PATTERN_LENGTH = 64
MAX_DECIMALS = 12


class ValueFormat(BaseModel):
    """Rendering rules for one column (or, at file level, for every column)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decimals: int | None = Field(default=None, ge=0, le=MAX_DECIMALS)
    strip_trailing_zeros: bool | None = None
    integer_min_decimals: int | None = Field(default=None, ge=0, le=MAX_DECIMALS)
    date_format: str | None = None
    timestamp_format: str | None = None
    null_text: str | None = None


class ExportFormat(ValueFormat):
    """File-level defaults plus per-column overrides."""

    columns: dict[str, ValueFormat] = Field(default_factory=dict)


@dataclass(frozen=True)
class _Rule:
    decimals: int | None
    strip_trailing_zeros: bool
    integer_min_decimals: int | None
    date_format: str | None
    timestamp_format: str | None
    null_text: str

    @classmethod
    def build(cls, defaults: ValueFormat, override: ValueFormat | None) -> "_Rule":
        def value(name: str, fallback: Any = None) -> Any:
            if override is not None and getattr(override, name) is not None:
                return getattr(override, name)
            got = getattr(defaults, name)
            return fallback if got is None else got

        return cls(
            decimals=value("decimals"),
            strip_trailing_zeros=bool(value("strip_trailing_zeros", False)),
            integer_min_decimals=value("integer_min_decimals"),
            date_format=value("date_format"),
            timestamp_format=value("timestamp_format"),
            null_text=str(value("null_text", "")),
        )


def validate_pattern(pattern: str, field: str) -> str:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise InvalidSchemaError(f"{field} is longer than {MAX_PATTERN_LENGTH} characters.", field="format_spec")
    unknown = sorted({d for d in _DIRECTIVE_RE.findall(pattern) if d not in ALLOWED_DIRECTIVES})
    if unknown:
        raise InvalidSchemaError(
            f"{field} uses unsupported directive(s) {['%' + d for d in unknown]}.",
            field="format_spec",
            details={"allowed_directives": sorted("%" + d for d in ALLOWED_DIRECTIVES)},
        )
    if pattern and not _DIRECTIVE_RE.search(pattern):
        raise InvalidSchemaError(f"{field} contains no strftime directive (for example %Y-%m-%d).", field="format_spec")
    return pattern


class ValueRenderer:
    """Applies a validated specification to the values of one relation."""

    def __init__(self, spec: ExportFormat, columns: list[str]) -> None:
        self.columns = list(columns)
        self.matched: dict[str, str] = {}  # specification key -> column name
        self.lenient: list[tuple[str, str, str]] = []  # key, column, how it matched
        overrides: dict[str, ValueFormat] = {}
        for key, rules in spec.columns.items():
            column, how = self._match(str(key), columns)
            if column in overrides:
                raise InvalidSchemaError(f"format_spec sets column {column!r} twice.", field="format_spec")
            overrides[column] = rules
            self.matched[str(key)] = column
            if how is not None:
                self.lenient.append((str(key), column, how))
        for rules, where in [(spec, "format_spec")] + [(r, f"format_spec.columns.{c}") for c, r in overrides.items()]:
            if rules.date_format is not None:
                validate_pattern(rules.date_format, f"{where}.date_format")
            if rules.timestamp_format is not None:
                validate_pattern(rules.timestamp_format, f"{where}.timestamp_format")
        self.rules = [_Rule.build(spec, overrides.get(name)) for name in columns]

    @staticmethod
    def _match(key: str, columns: list[str]) -> tuple[str, str | None]:
        """One column for a specification key: exact name first, then lenient forms.

        A lenient form that fits several columns is refused rather than guessed,
        so a key that exactly names one column can never be applied to another.
        """
        if key in columns:
            return key, None
        for candidates, how in (
            ([c for c in columns if c.casefold() == key.casefold()], "case-insensitive match"),
            ([c for c in columns if normalize(c) == normalize(key)], "normalized match"),
        ):
            if len(candidates) == 1:
                return candidates[0], how
            if len(candidates) > 1:
                raise InvalidSchemaError(
                    f"format_spec column {key!r} matches {len(candidates)} columns; name one exactly.",
                    field="format_spec", candidates=candidates,
                )
        raise InvalidSchemaError(
            f"format_spec references unknown column {key!r}.", field="format_spec", candidates=list(columns)
        )

    def render_row(self, row: Iterable[Any]) -> list[str]:
        return [self._render(value, rule) for value, rule in zip(row, self.rules)]

    # -- one value -------------------------------------------------------
    def _render(self, value: Any, rule: _Rule) -> str:
        if value is None:
            return rule.null_text
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float, Decimal)):
            return self._number(value, rule)
        if isinstance(value, dt.datetime):
            # Either key may name the pattern: asking for %Y-%m-%d on a timestamp
            # column is a rendering request, not a mistake to swallow silently.
            pattern = rule.timestamp_format or rule.date_format
            return value.strftime(pattern) if pattern else str(value)
        if isinstance(value, dt.date):
            pattern = rule.date_format or rule.timestamp_format
            return value.strftime(pattern) if pattern else str(value)
        if isinstance(value, dt.time):
            return str(value)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).hex()
        return str(value)

    @staticmethod
    def _number(value: int | float | Decimal, rule: _Rule) -> str:
        try:
            number = value if isinstance(value, Decimal) else Decimal(str(value))
        except InvalidOperation:  # pragma: no cover - defensive
            return str(value)
        if not number.is_finite():
            return str(value)
        if rule.decimals is not None:
            # Half-up on the shortest decimal form: 31783696.815 → 31783696.82,
            # where rounding the binary double itself would give .81.
            # Leave room for the integer digits, requested scale, and a rounding carry.
            # Do not let the caller's Decimal context constrain valid stored values.
            precision = max(1, number.adjusted() + 1) + rule.decimals + 1
            with localcontext(Context(prec=precision, rounding=ROUND_HALF_UP)):
                number = number.quantize(Decimal(1).scaleb(-rule.decimals))
        text = format(number, "f")
        if rule.strip_trailing_zeros and "." in text:
            text = text.rstrip("0").rstrip(".")
        if rule.integer_min_decimals is not None:
            whole, _, fraction = text.partition(".")
            if len(fraction) < rule.integer_min_decimals:
                text = whole + "." + fraction.ljust(rule.integer_min_decimals, "0")
        return text


def write_formatted_csv(path: Path, columns: list[str], rows: Iterable[Iterable[Any]], renderer: ValueRenderer) -> int:
    """Write rows as csv with a prepared renderer; returns the row count.

    Rendering happens in this process, so a formatted export materializes its
    rows in memory — exports are answers, not bulk unloads.
    """
    written = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow(renderer.render_row(row))
            written += 1
    return written
