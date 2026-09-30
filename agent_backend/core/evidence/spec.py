"""Evidence references: where imported rows and cells were read from (MADR 0021).

The agent reads a document, an image or a video outside the backend (MADR 0009) and imports what it read. A
reference links the imported rows and cells to a registered artifact and a place in it: a page, a text span, a
quote, a media time. This module owns the parts that need no storage: reading a loose reference into canonical
form, which locators an artifact kind can take, and checking a span or a quote against a document's text. The
backend checks what it holds facts for and records the rest as given; it never reads a PDF or a frame.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import InvalidIntentError
from ..models.entities import ArtifactKind

EVIDENCE_HINT = (
    'evidence is a list of references, one per place the values were read from: {"artifact": "report.md", '
    '"rows": [0, 2], "columns": ["revenue"], "quote": "Revenue rose to 4.2m"}. artifact is a registered '
    "artifact's id or name, or a file path, which is registered; content_hash, if given, must be the registered "
    "one. rows are 0-based positions of the imported rows (all rows when left out) and columns their names (all "
    "columns when left out). A place is page (1 or more; documents), span ([start, end] character offsets in a "
    "text), quote (the text read), time_s (seconds, or [start, end]; audio and video) and note (free text).")
_KEYS = ("artifact", "content_hash", "rows", "row", "columns", "column", "page", "span", "quote", "time_s", "note")
_NO_PAGES = (ArtifactKind.VIDEO, ArtifactKind.AUDIO, ArtifactKind.IMAGE)
_TIMED = (ArtifactKind.VIDEO, ArtifactKind.AUDIO, ArtifactKind.OTHER)


@dataclass(frozen=True)
class EvidenceSpec:
    """One reference read into canonical form, before its artifact is resolved."""

    index: int
    artifact: str
    content_hash: str | None = None
    rows: list[int] | None = None
    columns: list[str] | None = None
    page: int | None = None
    span: tuple[int, int] | None = None
    quote: str | None = None
    time_s: tuple[float, float] | None = None
    note: str = ""

    @property
    def field(self) -> str:
        return f"evidence[{self.index}]"

    def locator(self) -> dict[str, Any]:
        """The place in the artifact, as stored and returned."""
        found: dict[str, Any] = {}
        if self.page is not None:
            found["page"] = self.page
        if self.span is not None:
            found["span"] = list(self.span)
        if self.quote is not None:
            found["quote"] = self.quote
        if self.time_s is not None:
            found["time_s"] = self.time_s[0] if self.time_s[0] == self.time_s[1] else list(self.time_s)
        return found


@dataclass
class TextCheck:
    """What checking a span or a quote against a document's text established."""

    checked: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    span: tuple[int, int] | None = None  # a span the backend located for a quote given without one
    occurrences: int | None = None


def _refuse(message: str, where: str, **details: Any) -> InvalidIntentError:
    return InvalidIntentError(message, field=where, hint=EVIDENCE_HINT, details=details or None)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_evidence(loose: Any) -> list[EvidenceSpec]:
    """Read the evidence argument of an import; shape only, nothing is looked up."""
    if loose is None or loose == []:
        return []
    if isinstance(loose, dict):
        loose = [loose]
    if not isinstance(loose, list):
        raise _refuse("evidence must be a list of references.", "evidence")
    return [_parse_one(index, item) for index, item in enumerate(loose)]


def _parse_one(index: int, item: Any) -> EvidenceSpec:
    where = f"evidence[{index}]"
    if not isinstance(item, dict):
        raise _refuse(f"Reference {index + 1} is not an object.", where)
    unknown = sorted(k for k in item if k not in _KEYS)
    if unknown:
        raise _refuse(f"Unknown key(s) {unknown} in reference {index + 1}.", where, allowed_keys=list(_KEYS))
    artifact = item.get("artifact")
    if not isinstance(artifact, str) or not artifact.strip():
        raise _refuse(f"Reference {index + 1} names no artifact.", f"{where}.artifact")
    content_hash = item.get("content_hash")
    if content_hash is not None:
        if not isinstance(content_hash, str) or not content_hash.strip():
            raise _refuse("content_hash must be the artifact's sha256 as hex text.", f"{where}.content_hash")
        content_hash = content_hash.strip().lower().removeprefix("sha256:")
    rows = _positions(item, where)
    columns = _names(item, where)
    page = item.get("page")
    if page is not None and (not isinstance(page, int) or isinstance(page, bool) or page < 1):
        raise _refuse(f"page must be a whole number of 1 or more, got {page!r}.", f"{where}.page")
    span = item.get("span")
    if span is not None:
        if (not isinstance(span, list) or len(span) != 2 or not all(isinstance(v, int) and not isinstance(v, bool)
                                                                     for v in span)
                or span[0] < 0 or span[0] >= span[1]):
            raise _refuse(f"span must be [start, end] character offsets with 0 <= start < end, got {span!r}.",
                          f"{where}.span")
        span = (span[0], span[1])
    quote = item.get("quote")
    if quote is not None and (not isinstance(quote, str) or not quote.strip()):
        raise _refuse("quote must be the text read, as a non-empty string.", f"{where}.quote")
    time_s = item.get("time_s")
    if time_s is not None:
        if _number(time_s):
            time_s = (float(time_s), float(time_s))
        elif isinstance(time_s, list) and len(time_s) == 2 and all(_number(v) for v in time_s):
            time_s = (float(time_s[0]), float(time_s[1]))
        else:
            raise _refuse(f"time_s must be seconds or [start, end] in seconds, got {time_s!r}.", f"{where}.time_s")
        if time_s[0] < 0 or time_s[0] > time_s[1]:
            raise _refuse(f"time_s must satisfy 0 <= start <= end, got {list(time_s)}.", f"{where}.time_s")
    note = item.get("note", "")
    if not isinstance(note, str):
        raise _refuse("note must be text.", f"{where}.note")
    return EvidenceSpec(index=index, artifact=artifact.strip(), content_hash=content_hash, rows=rows,
                        columns=columns, page=page, span=span, quote=quote, time_s=time_s, note=note.strip())


def _positions(item: dict[str, Any], where: str) -> list[int] | None:
    if "rows" in item and "row" in item:
        raise _refuse("Give rows or row, not both.", f"{where}.rows")
    raw = item.get("rows", item.get("row"))
    if raw is None:
        return None
    raw = [raw] if isinstance(raw, int) and not isinstance(raw, bool) else raw
    if (not isinstance(raw, list) or not raw
            or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in raw)):
        raise _refuse(f"rows must be a non-empty list of 0-based row positions, got {raw!r}.", f"{where}.rows")
    return sorted(set(raw))


def _names(item: dict[str, Any], where: str) -> list[str] | None:
    if "columns" in item and "column" in item:
        raise _refuse("Give columns or column, not both.", f"{where}.columns")
    raw = item.get("columns", item.get("column"))
    if raw is None:
        return None
    raw = [raw] if isinstance(raw, str) else raw
    if not isinstance(raw, list) or not raw or not all(isinstance(v, str) and v.strip() for v in raw):
        raise _refuse(f"columns must be a non-empty list of column names, got {raw!r}.", f"{where}.columns")
    return list(dict.fromkeys(v.strip() for v in raw))


def check_kind(spec: EvidenceSpec, kind: ArtifactKind, name: str) -> None:
    """Refuse a place the artifact's kind cannot have: a page of a video, a time in a document."""
    if spec.page is not None and kind in _NO_PAGES:
        raise _refuse(f"{name} is a {kind} artifact, which has no pages; a place in it is time_s.",
                      f"{spec.field}.page", kind=str(kind))
    if spec.time_s is not None and kind not in _TIMED:
        raise _refuse(f"{name} is a {kind} artifact, which has no time; a place in it is page, span or quote.",
                      f"{spec.field}.time_s", kind=str(kind))


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def check_text(spec: EvidenceSpec, text: str, name: str) -> TextCheck:
    """Check a span and a quote against a document's text; refuse what the text contradicts.

    A span must lie within the text; a quote given with a span must equal the text there, whitespace collapsed;
    a quote given alone must occur in the text, and is located when it occurs exactly once.
    """
    found = TextCheck()
    if spec.span is not None:
        start, end = spec.span
        if end > len(text):
            raise _refuse(f"span {list(spec.span)} runs past the end of {name}, which holds {len(text)} characters.",
                          f"{spec.field}.span", length=len(text))
        found.checked.append("span")
        if spec.quote is not None:
            at = text[start:end]
            if _collapsed(at) != _collapsed(spec.quote):
                raise _refuse(f"The quote does not match the text of {name} at span {list(spec.span)}.",
                              f"{spec.field}.quote", text_at_span=at[:300])
            found.checked.append("quote")
        return found
    if spec.quote is not None:
        words = spec.quote.split()
        pattern = re.compile(r"\s+".join(re.escape(w) for w in words))
        matches = list(pattern.finditer(text))
        if not matches:
            raise _refuse(f"The quote does not occur in {name}.", f"{spec.field}.quote")
        found.checked.append("quote")
        found.occurrences = len(matches)
        if len(matches) == 1:
            found.span = matches[0].span()
    return found
