"""attach_metadata: a knowledge.md semantic layer becomes dataset/column metadata."""

from pathlib import Path

import pytest

from agent_backend.core.knowledge import parse_knowledge_markdown
from tests.conftest import write_csv

KNOWLEDGE_TABLE_STYLE = """# Knowledge Guide: cn-origin-ccks_macro

## 1. Database Overview

Macro statistics. Key Fields: none here.

## 2. Core Business Entities

### 2.1 Monetary Authority Balance Sheet (`ed_moneyauthoritybs`)

Represents the consolidated balance sheet of China's central bank.

| Column | Semantic Definition | Unit |
|---|---|---|
| `totalassets` | Total assets held by the monetary authority | 亿元 (100M CNY) |
| `forex` | Foreign exchange reserves held as assets | 亿元 |
| `enddate` | Reporting period end date | DATE |
| `ghost` | A column that does not exist | |

### 2.2 Retail Sales (ed_retail)

Monthly retail sales of consumer goods.

| Field | Semantic Definition |
|---|---|
| `retailvalue` | Retail value, unit: 亿元 |
"""

KNOWLEDGE_BULLET_STYLE = """# Knowledge Guide: fund

## 2. Core Entities & Field Definitions

### 2.1 Fund Archives

| Table | Business Purpose |
|-------|-----------------|
| `mf_fundarchives` | Master fund profile |
| `mf_missing` | A table we do not have |

Key Fields:
- `innercode` — Internal fund identifier; primary join key.
- `secuabbr` / `secucode` — Short name and public code.

### 2.2 Performance

| 表名 | 字段 | 语义定义 |
|------|------|----------|
| `mf_netvalueperformancehis` | `rrintenyear` | 近十年累计回报率，单位：百分比（%） |
| `mf_fundarchives` | `secucode` | 基金对外公示代码（如 000001） |
"""


def test_parser_extracts_scoped_facts():
    doc = parse_knowledge_markdown(KNOWLEDGE_TABLE_STYLE)
    assert {t.name for t in doc.tables} == {"ed_moneyauthoritybs", "ed_retail"}
    facts = {(c.scope, c.name): c for c in doc.columns}
    assert facts[("ed_moneyauthoritybs", "totalassets")].unit == "亿元 (100M CNY)"
    assert facts[("ed_moneyauthoritybs", "enddate")].description == "Reporting period end date"
    assert facts[("ed_retail", "retailvalue")].unit == "亿元"

    doc = parse_knowledge_markdown(KNOWLEDGE_BULLET_STYLE)
    assert {t.name for t in doc.tables} == {"mf_fundarchives", "mf_missing"}
    names = {(c.scope, c.name) for c in doc.columns}
    assert (None, "innercode") in names and (None, "secuabbr") in names and (None, "secucode") in names
    assert ("mf_netvalueperformancehis", "rrintenyear") in names
    assert next(c for c in doc.columns if c.name == "rrintenyear").unit == "百分比"


@pytest.fixture
def macro(backend, tmp_path: Path):
    write_csv(tmp_path / "ed_moneyauthoritybs.csv", "EndDate,TotalAssets,Forex", ["2002-01-31,4531103.0,1"])
    write_csv(tmp_path / "ed_retail.csv", "EndDate,RetailValue", ["2002-01-31,12"])
    for name in ("ed_moneyauthoritybs.csv", "ed_retail.csv"):
        assert backend.import_dataset(str(tmp_path / name))["status"] == "success"
    knowledge = tmp_path / "knowledge.md"
    knowledge.write_text(KNOWLEDGE_TABLE_STYLE, encoding="utf-8")
    return knowledge


def test_attach_metadata_applies_descriptions_and_units(backend, macro):
    response = backend.attach_metadata(str(macro))
    assert response["status"] == "success", response
    applied = {a["name"]: a for a in response["applied"]}
    assert applied["ed_moneyauthoritybs"]["description_set"] is True
    assert sorted(applied["ed_moneyauthoritybs"]["columns"]) == ["EndDate", "Forex", "TotalAssets"]
    assert applied["ed_retail"]["columns"] == ["RetailValue"]
    assert [u["name"] for u in response["unmatched"]["columns"]] == ["ghost"]
    assert response["unmatched"]["tables"] == []

    described = backend.describe_dataset("ed_moneyauthoritybs")
    assert described["dataset"]["description"].startswith("Represents the consolidated balance sheet")
    total = next(c for c in described["schema"] if c["name"] == "TotalAssets")
    assert total["unit"] == "亿元 (100M CNY)"
    assert total["description"] == "Total assets held by the monetary authority"
    assert described["hints"]["units"]["TotalAssets"] == "亿元 (100M CNY)"
    assert described["dataset"]["metadata"]["knowledge_artifacts"] == [response["source"]["id"]]
    assert backend.list_artifacts(kind="markdown")["count"] == 1


def test_attach_metadata_respects_existing_unless_overwrite(backend, macro):
    backend.update_metadata("ed_retail", description="Hand-written", columns={"RetailValue": {"unit": "CNY"}})
    backend.attach_metadata(str(macro))
    described = backend.describe_dataset("ed_retail")
    assert described["dataset"]["description"] == "Hand-written"
    assert next(c for c in described["schema"] if c["name"] == "RetailValue")["unit"] == "CNY"
    backend.attach_metadata(str(macro), overwrite=True)
    described = backend.describe_dataset("ed_retail")
    assert described["dataset"]["description"] == "Monthly retail sales of consumer goods."
    assert next(c for c in described["schema"] if c["name"] == "RetailValue")["unit"] == "亿元"


def test_attach_metadata_scoped_to_one_dataset(backend, macro):
    response = backend.attach_metadata(str(macro), dataset="ed_retail")
    assert [a["name"] for a in response["applied"]] == ["ed_retail"]
    assert {u["name"] for u in response["unmatched"]["tables"]} == {"ed_moneyauthoritybs"}


def test_attach_metadata_source_forms_and_errors(backend, macro):
    first = backend.attach_metadata(str(macro))
    artifact_id = first["source"]["id"]
    again = backend.attach_metadata(artifact_id)
    assert again["status"] == "success" and again["source"]["id"] == artifact_id
    by_name = backend.attach_metadata("knowledge.md")
    assert by_name["source"]["id"] == artifact_id
    assert backend.attach_metadata("/nonexistent.md")["code"] == "NOT_FOUND"
    assert backend.attach_metadata(str(macro.parent / "ed_retail.csv"))["code"] == "INVALID_SCHEMA"


def test_attach_metadata_after_workspace_import(backend, tmp_path: Path):
    root = tmp_path / "context"
    (root / "csv").mkdir(parents=True)
    write_csv(root / "csv" / "mf_fundarchives.csv", "innercode,secucode,secuabbr", ["1,000001,基金A"])
    write_csv(root / "csv" / "mf_netvalueperformancehis.csv", "innercode,rrintenyear", ["1,166.09"])
    (root / "knowledge.md").write_text(KNOWLEDGE_BULLET_STYLE, encoding="utf-8")
    imported = backend.import_workspace(str(root))
    assert imported["status"] == "success", imported
    response = backend.attach_metadata("knowledge.md")
    assert response["status"] == "success", response
    applied = {a["name"]: a for a in response["applied"]}
    assert applied["mf_fundarchives"]["description_set"] is True
    assert sorted(applied["mf_fundarchives"]["columns"]) == ["innercode", "secuabbr", "secucode"]
    assert applied["mf_netvalueperformancehis"]["columns"] == ["innercode", "rrintenyear"]
    assert [t["name"] for t in response["unmatched"]["tables"]] == ["mf_missing"]
    described = backend.describe_dataset("mf_netvalueperformancehis")
    assert next(c for c in described["schema"] if c["name"] == "rrintenyear")["unit"] == "百分比"
    assert next(c for c in described["schema"] if c["name"] == "innercode")["description"].startswith("Internal fund identifier")
    events = [e["event"] for e in backend.get_provenance("mf_fundarchives")["audit"]]
    assert events[-1] == "metadata.attached"
    assert backend.integrity_report()["ok"]
