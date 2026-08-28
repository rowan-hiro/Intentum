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
        "list_datasets", "describe_dataset", "search_datasets", "import_dataset", "transform_dataset",
        "materialize_result", "publish_dataset", "update_metadata", "delete_dataset", "restore_dataset",
        "get_provenance", "get_operation",
    }
    assert not (names & FORBIDDEN)
    transform = next(t for t in tools if t.name == "transform_dataset")
    assert set(transform.input_schema["required"]) == {"source", "transform"}
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
