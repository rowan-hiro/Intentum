"""Heuristic parser for knowledge / semantic-layer markdown documents.

Real workspaces ship a ``knowledge.md`` that describes tables and columns in
prose, markdown tables and bullet lists, in English or Chinese, with no fixed
schema. The parser extracts *facts* (a table has a description; a column has a
description and a unit, optionally scoped to a table) without deciding which
dataset they belong to. Matching facts to datasets is the backend's job, and
whatever does not match is reported back to the agent rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_TRAILING_PAREN_ID_RE = re.compile(r"[（(]\s*([\w.\-]+)\s*[)）]\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.*)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_BULLET_ID_LIST_RE = re.compile(r"^((?:`[^`]+`\s*(?:/|,|、|或|and|or)?\s*)+)(?:[—–\-:：]+|\s+[—–\-:：]\s+)\s*(.+)$")
_UNIT_IN_TEXT_RE = re.compile(r"(?:unit|units|单位)\s*[:：]\s*([^\s()（）,，;；。]+)", re.IGNORECASE)

COLUMN_HEADERS = {"column", "field", "column name", "field name", "columns", "fields", "attribute",
                  "字段", "字段名", "字段名称", "列", "列名", "列名称", "属性"}
TABLE_HEADERS = {"table", "table name", "tables", "table(s)", "entity", "relation", "dataset",
                 "表", "表名", "数据表", "表名称", "实体"}
DESCRIPTION_HEADERS = ["semantic definition", "definition", "description", "meaning", "semantics", "semantic",
                       "business purpose", "purpose", "business role", "role", "usage", "context", "notes",
                       "含义", "语义", "说明", "描述", "用途", "业务含义", "定义", "换算说明", "备注"]
UNIT_HEADERS = {"unit", "units", "单位", "计量单位"}
SCOPE_HEADERS = {"table", "table(s)", "tables", "table name", "表", "表名", "表名称", "数据表", "所属表", "来源表"}


@dataclass
class TableFact:
    name: str
    description: str
    line: int


@dataclass
class ColumnFact:
    name: str
    description: str
    unit: str
    scope: str | None  # table identifier the fact is scoped to, when known
    line: int


@dataclass
class KnowledgeDocument:
    title: str = ""
    tables: list[TableFact] = field(default_factory=list)
    columns: list[ColumnFact] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {"tables": len(self.tables), "columns": len(self.columns)}


def _clean_cell(cell: str) -> str:
    return cell.strip().strip("*").strip()


def _header_key(cell: str) -> str:
    return _clean_cell(cell).strip("`").lower()


def _identifiers(cell: str) -> list[str]:
    """Identifiers named in a cell: backticked ones, or the bare cell."""
    ticked = [t.strip() for t in _BACKTICK_RE.findall(cell)]
    if ticked:
        return [t for t in ticked if t]
    bare = _clean_cell(cell).strip("`")
    if not bare or bare in ("-", "—", "N/A"):
        return []
    parts = [p.strip() for p in re.split(r"\s*(?:/|,|、)\s*", bare)]
    return [p for p in parts if p and " " not in p] or [bare]


def _heading_scope(text: str) -> str | None:
    ticked = _BACKTICK_RE.findall(text)
    if ticked:
        return ticked[0].strip()
    match = _TRAILING_PAREN_ID_RE.search(text)
    if match:
        candidate = match.group(1)
        # "(NFA)" or "(Conditional)" are abbreviations, not table identifiers.
        if "_" in candidate or candidate == candidate.lower():
            return candidate
    return None


def _find_index(headers: list[str], candidates: list[str] | set[str]) -> int | None:
    ordered = list(candidates) if isinstance(candidates, list) else sorted(candidates)
    for candidate in ordered:
        for index, header in enumerate(headers):
            if header == candidate or header.startswith(candidate):
                return index
    return None


def parse_knowledge_markdown(text: str) -> KnowledgeDocument:
    doc = KnowledgeDocument()
    lines = text.splitlines()
    scope: str | None = None
    scope_level = 0
    awaiting_description: str | None = None  # table id whose section paragraph is still to come
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2)
            if not doc.title and level == 1:
                doc.title = re.sub(r"`", "", title).strip()
            if level <= scope_level:
                scope = None
                scope_level = 0
            # The document title (h1) names the database, never a table.
            new_scope = _heading_scope(title) if level >= 2 else None
            if new_scope:
                scope, scope_level = new_scope, level
                awaiting_description = new_scope
            else:
                awaiting_description = None
            index += 1
            continue

        table_row = _TABLE_ROW_RE.match(line)
        if table_row and index + 1 < len(lines) and _TABLE_SEP_RE.match(lines[index + 1]):
            headers = [_header_key(c) for c in table_row.group(1).split("|")]
            index += 2
            rows: list[tuple[int, list[str]]] = []
            while index < len(lines):
                row = _TABLE_ROW_RE.match(lines[index])
                if not row:
                    break
                rows.append((index + 1, [c for c in row.group(1).split("|")]))
                index += 1
            _consume_table(doc, headers, rows, scope)
            awaiting_description = None
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            _consume_bullet(doc, bullet.group(1).strip(), scope, index + 1)
            index += 1
            continue

        if awaiting_description and stripped and not stripped.startswith(("|", "```", "<!--", "---", "***", "___")):
            description = re.sub(r"\s+", " ", stripped)
            if description.lower().startswith(("key fields", "关键字段", "主要字段")):
                awaiting_description = None
            else:
                doc.tables.append(TableFact(name=awaiting_description, description=description[:500], line=index + 1))
                awaiting_description = None
        index += 1
    return doc


def _consume_table(doc: KnowledgeDocument, headers: list[str], rows: list[tuple[int, list[str]]], scope: str | None) -> None:
    if not headers or not rows:
        return
    first = headers[0]
    desc_index = _find_index(headers, DESCRIPTION_HEADERS)
    name_index = next((i for i, h in enumerate(headers) if h in COLUMN_HEADERS), None)
    if name_index is None:
        name_index = next((i for i, h in enumerate(headers) if h.startswith(("column", "field", "字段"))), None)
    if name_index is not None:
        unit_index = _find_index(headers, UNIT_HEADERS)
        scope_index = next((i for i, h in enumerate(headers) if i != name_index and h in SCOPE_HEADERS), None)
        if desc_index is None or desc_index == name_index:
            desc_index = next((i for i in range(len(headers)) if i not in (name_index, scope_index, unit_index)), None)
        for line_no, cells in rows:
            if name_index >= len(cells):
                continue
            description = _clean_cell(cells[desc_index]) if desc_index is not None and desc_index < len(cells) else ""
            unit = _clean_cell(cells[unit_index]) if unit_index is not None and unit_index < len(cells) else ""
            if not unit:
                found = _UNIT_IN_TEXT_RE.search(description)
                unit = found.group(1) if found else ""
            row_scope = scope
            if scope_index is not None and scope_index < len(cells):
                ids = _identifiers(cells[scope_index])
                row_scope = ids[0] if len(ids) == 1 else None
            for name in _identifiers(cells[name_index]):
                if row_scope is None and "." in name and " " not in name:
                    row_scope, name = name.rsplit(".", 1)
                doc.columns.append(ColumnFact(name=name, description=description, unit=unit, scope=row_scope, line=line_no))
        return
    if first in TABLE_HEADERS or first.startswith(("table", "表")):
        if desc_index is None:
            desc_index = 1 if len(headers) > 1 else None
        for line_no, cells in rows:
            if not cells:
                continue
            description = _clean_cell(cells[desc_index]) if desc_index is not None and desc_index < len(cells) else ""
            for name in _identifiers(cells[0]):
                doc.tables.append(TableFact(name=name, description=description, line=line_no))


def _consume_bullet(doc: KnowledgeDocument, content: str, scope: str | None, line_no: int) -> None:
    match = _BULLET_ID_LIST_RE.match(content)
    if not match:
        return
    names = [n.strip() for n in _BACKTICK_RE.findall(match.group(1)) if n.strip()]
    description = _clean_cell(match.group(2))
    if not names or not description:
        return
    found = _UNIT_IN_TEXT_RE.search(description)
    unit = found.group(1) if found else ""
    for name in names:
        doc.columns.append(ColumnFact(name=name, description=description, unit=unit, scope=scope, line=line_no))
