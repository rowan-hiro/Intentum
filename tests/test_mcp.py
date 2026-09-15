import asyncio

import pytest

from agent_backend.mcp.server import create_server
from tests.conftest import ORDERS_CSV

FORBIDDEN = {"insert_row", "update_table", "execute_sql", "write_json", "edit_metadata_file", "query", "sql"}


@pytest.fixture
def server(backend):
    return create_server(backend)


def call(server, tool, **args):
    result = asyncio.run(server.call_tool(tool, args))
    assert result.structured_content is not None
    return result.structured_content


def test_tool_surface_is_semantic(server):
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "list_datasets", "list_artifacts", "describe_dataset", "search_datasets", "import_dataset",
        "import_workspace", "attach_metadata", "transform_dataset", "materialize_result", "export_result",
        "publish_dataset", "update_metadata", "delete_dataset", "restore_dataset", "get_provenance", "get_operation",
        "declare_output", "get_output_contract",
    }
    assert not (names & FORBIDDEN)
    transform = next(t for t in tools if t.name == "transform_dataset")
    assert set(transform.input_schema["required"]) == {"source"}  # an absent transform is the backend's to refuse
    delete = next(t for t in tools if t.name == "delete_dataset")
    assert delete.annotations.destructive_hint is True


def test_end_to_end_through_mcp(server):
    imported = call(server, "import_dataset", path=str(ORDERS_CSV), description="Orders")
    assert imported["status"] == "success"
    created = call(server, "materialize_result", source="the orders I imported today",
                   transform={"group_by": ["region"], "metric": "revenue"}, name="regional_sales")
    assert created["status"] == "success", created
    assert created["dataset"]["columns"] == ["region", "revenue"]
    assert created["lineage"] == ["ds_1"]
    replay = call(server, "materialize_result", source="orders",
                  transform={"group_by": ["region"], "metric": "revenue"}, name="regional_sales")
    assert replay["idempotent_replay"] is True
    prov = call(server, "get_provenance", dataset="regional_sales")
    assert prov["inputs"][0]["name"] == "orders"


def test_errors_are_structured_not_raised(server):
    response = call(server, "describe_dataset", dataset="missing")
    assert response["status"] == "error" and response["code"] == "NOT_FOUND"


def test_output_contract_through_mcp(server, tmp_path):
    from tests.conftest import ORDERS_CSV

    assert call(server, "import_dataset", path=str(ORDERS_CSV))["status"] == "success"
    declared = call(server, "declare_output", columns=["region", "revenue"], rows={"one_per": ["region"]})
    assert declared["status"] == "success" and declared["contract"]["id"] == "oc_1", declared
    assert call(server, "declare_output", columns="region", rows="at_least_one")["code"] == "CONFLICT"
    call(server, "materialize_result", source="orders", transform={"group_by": ["region"], "measures": ["revenue", "count"]}, name="wide")
    refused = call(server, "export_result", dataset="wide", path=str(tmp_path / "p.csv"))
    assert refused["code"] == "CONTRACT_MISMATCH" and refused["details"]["repair"] == {"select": ["region", "revenue"]}
    call(server, "materialize_result", source="wide", transform=refused["details"]["repair"], name="answer")
    assert call(server, "export_result", dataset="answer", path=str(tmp_path / "p.csv"))["contract"]["status"] == "satisfied"
    shown = call(server, "get_output_contract")
    assert [e["event"] for e in shown["history"]] == ["output_contract.declared", "output_contract.satisfied"]


def test_a_loose_argument_is_refused_by_the_backend_with_advice_not_by_the_sdk(server):
    """A transform sent as malformed JSON text, or a source sent as a join object, reaches the backend (MADR 0010)."""
    assert call(server, "import_dataset", path=str(ORDERS_CSV))["status"] == "success"
    broken = call(server, "transform_dataset", source="orders", transform='[{"filter": "amount > 1"')
    assert broken["status"] == "error" and broken["code"] == "INVALID_TRANSFORM"
    assert [a["kind"] for a in broken["advice"]] == ["transform_as_text"]
    assert "not JSON" in broken["advice"][0]["explanation"]
    joined = call(server, "transform_dataset", source={"left": "orders", "right": "orders", "on": {"order_id": "order_id"}},
                  transform={"limit": 1})
    assert joined["status"] == "error"
    assert joined["advice"][0]["kind"] == "source_as_dataset" and joined["advice"][0]["rewrite"][0]["arguments"]["source"] == "orders"
    parsed = call(server, "transform_dataset", source="orders", transform='{"limit": 1}')  # valid JSON text is pre-parsed
    assert parsed["status"] == "success"


def test_describe_dataset_carries_the_column_profile_through_the_server(server):
    assert call(server, "import_dataset", path=str(ORDERS_CSV))["status"] == "success"
    described = call(server, "describe_dataset", dataset="orders", sample_rows=0)
    region = next(c for c in described["schema"] if c["name"] == "region")
    assert (region["non_null"], region["distinct"]) == (12, 4)
    plain = call(server, "describe_dataset", dataset="orders", profile=False)
    assert "non_null" not in next(c for c in plain["schema"] if c["name"] == "region")


def test_a_call_without_its_transform_is_refused_by_the_backend_with_advice(server):
    assert call(server, "import_dataset", path=str(ORDERS_CSV))["status"] == "success"
    for tool, extra in (("transform_dataset", {}), ("materialize_result", {"name": "orders_copy"})):
        response = call(server, tool, source="orders", **extra)
        assert response["status"] == "error" and response["code"] == "INVALID_INTENT", response
        assert [a["kind"] for a in response["advice"]] == ["transform_required"]
        assert "empty list" in response["advice"][0]["explanation"]
    assert not any(d["name"] == "orders_copy" for d in call(server, "list_datasets")["datasets"])
    # A query written as the source, with no transform at all, comes back as a runnable request.
    queried = call(server, "transform_dataset",
                   source={"raw_query": {"sql": "SELECT count(*) AS n FROM o", "inputs": {"o": "orders"}}})
    (rewrite,) = queried["advice"][0]["rewrite"]
    assert rewrite["arguments"]["source"] == "orders" and rewrite["arguments"]["transform"][0]["raw_query"]["sql"].startswith("SELECT")
    assert call(server, rewrite["tool"], **rewrite["arguments"])["result"]["rows"] == [[12]]


def test_parallel_tool_calls_through_the_server_never_fail_internally(server):
    """An agent that calls several tools in one step: the SDK runs the handlers on worker threads."""
    assert call(server, "import_dataset", path=str(ORDERS_CSV))["status"] == "success"

    async def burst():
        calls = []
        for i in range(30):
            if i % 2:
                calls.append(server.call_tool("describe_dataset", {"dataset": "orders", "sample_rows": 3}))
            else:
                calls.append(server.call_tool("transform_dataset", {"source": "orders", "transform": {"limit": 2}}))
        return await asyncio.gather(*calls)

    results = asyncio.run(burst())
    bodies = [r.structured_content for r in results]
    assert all(b is not None and b["status"] == "success" for b in bodies), [b for b in bodies if not b or b["status"] != "success"]
