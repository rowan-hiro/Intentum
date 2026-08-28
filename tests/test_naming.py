"""Unicode-safe identifiers: Chinese table/column names import, resolve and execute."""

import pytest

from agent_backend.core.naming import is_identifier, normalize, slugify, tokens
from tests.conftest import write_csv


def test_normalize_and_slugify_unicode():
    assert slugify("公募基金经理(新)") == "公募基金经理_新"
    assert slugify("Regional Sales") == "regional_sales"
    assert slugify("2024 报表") == "d_2024_报表"
    assert normalize("A股证券代码") == "a股证券代码"
    for text in ("公募基金经理(新)", "Regional Sales", "ÉTÉ 2024", "基金_简称"):
        assert slugify(slugify(text)) == slugify(text)
    assert is_identifier("regional_sales") and is_identifier("公募基金经理_新")
    assert not is_identifier("Regional Sales") and not is_identifier("")


def test_slugify_rejects_empty():
    from agent_backend.core.errors import InvalidIntentError

    with pytest.raises(InvalidIntentError):
        slugify("()")


def test_tokens_emit_cjk_bigrams():
    assert "基金" in tokens("基金简称") and "简称" in tokens("基金简称") and "基金简称" in tokens("基金简称")
    assert tokens("A股证券代码")[:2] == ["a", "股证券代码"]
    assert tokens("regional_sales") == ["regional", "sales"]


@pytest.fixture
def chinese(backend, tmp_path):
    path = write_csv(tmp_path / "公司股本结构变动.csv", "公司代码,公司中文名称,配股年度,募集资金总额(元)",
                     ["1,北京公司,1995,100.5", "2,上海公司,1997,200.25", "3,北京公司,1997,50"])
    response = backend.import_dataset(str(path))
    assert response["status"] == "success", response
    return response["dataset"]


def test_chinese_names_import_and_describe(backend, chinese):
    assert chinese["name"] == "公司股本结构变动"
    described = backend.describe_dataset("公司股本结构变动")
    assert described["status"] == "success"
    roles = {c["name"]: c["role"] for c in described["schema"]}
    assert roles["公司代码"] == "identifier"
    assert roles["配股年度"] == "time"
    assert roles["募集资金总额(元)"] == "measure"


def test_chinese_dataset_reference_forms(backend, chinese):
    for ref in ("公司股本结构变动", "公司股本结构变动.csv", "股本结构", "那个股本结构变动的表"):
        response = backend.describe_dataset(ref)
        assert response["status"] == "success", (ref, response)
        assert response["dataset"]["id"] == chinese["id"]


def test_chinese_columns_resolve_and_execute(backend, chinese):
    response = backend.transform_dataset(
        "公司股本结构变动",
        {"filter": "配股年度 = 1997", "group_by": ["公司中文名称"], "measures": [{"sum": "募集资金总额(元)", "as": "募集总额"}],
         "sort": "-募集总额"},
        explain=True,
    )
    assert response["status"] == "success", response
    assert [c["name"] for c in response["result"]["columns"]] == ["公司中文名称", "募集总额"]
    assert response["result"]["rows"] == [["上海公司", 200.25], ["北京公司", 50.0]]
    assert '"募集资金总额(元)"' in response["explain"]["sql"]


def test_chinese_column_fuzzy_and_ambiguity(backend, chinese):
    fuzzy = backend.transform_dataset("公司股本结构变动", {"select": ["公司中文名"]})
    assert fuzzy["status"] == "success", fuzzy
    assert fuzzy["resolution"][0]["resolved_to"] == "公司中文名称"
    missing = backend.transform_dataset("公司股本结构变动", {"select": ["注册资本"]})
    assert missing["code"] == "NOT_FOUND"
    assert "公司代码" in [c["name"] for c in missing["candidates"]]


def test_chinese_derived_and_materialized_names(backend, chinese):
    created = backend.materialize_result(
        "公司股本结构变动", {"derive": {"募集(万元)": "募集资金总额(元) / 10000"}, "select": ["公司代码", "募集(万元)"]},
        "募集 明细", description="按万元计的募集资金",
    )
    assert created["status"] == "success", created
    assert created["dataset"]["name"] == "募集_明细"
    assert created["dataset"]["columns"] == ["公司代码", "募集_万元"]
    assert any(n["reference"] == "募集 明细" for n in created["resolution"])
    assert backend.describe_dataset("募集明细")["status"] == "success"
    assert backend.integrity_report()["ok"]


@pytest.mark.parametrize(("predicate", "expected_notes"), [
    ("note = 'Unit Price'", ["Unit Price"]),
    ("note = 'A ''Unit Price'' label'", ["A 'Unit Price' label"]),
    ('"Extended Unit Price Label" = 20', ["Unit Price"]),
    ("Unit Price + Extended Unit Price Label = 30", ["Unit Price"]),
])
def test_special_fields_preserve_literals_and_quoted_identifiers(backend, tmp_path, predicate, expected_notes):
    path = write_csv(tmp_path / "prices.csv", "Unit Price,Extended Unit Price Label,note",
                     ["10,20,Unit Price", "11,21,A 'Unit Price' label"])
    assert backend.import_dataset(str(path))["status"] == "success"

    response = backend.transform_dataset("prices", {"filter": predicate, "select": ["note"]})

    assert response["status"] == "success", response
    assert response["result"]["rows"] == [[note] for note in expected_notes]


def test_chinese_temporal_hint_words(backend, clock, tmp_path):
    backend.import_dataset(str(write_csv(tmp_path / "北京股本变动.csv", "公司代码,配股年度", ["1,2001"])))
    clock.advance(days=1)
    backend.import_dataset(str(write_csv(tmp_path / "上海股本变动.csv", "公司代码,配股年度", ["9,2001"])))
    ambiguous = backend.describe_dataset("股本变动")
    assert ambiguous["status"] == "needs_resolution", ambiguous
    assert {c["name"] for c in ambiguous["candidates"]} == {"北京股本变动", "上海股本变动"}
    today = backend.describe_dataset("今天导入的股本变动")
    assert today["status"] == "success", today
    assert today["dataset"]["name"] == "上海股本变动"
    latest = backend.describe_dataset("最新的股本变动")
    assert latest["status"] == "success" and latest["dataset"]["name"] == "上海股本变动"
    assert any("most recently created" in n["reason"] for n in latest["resolution"])
