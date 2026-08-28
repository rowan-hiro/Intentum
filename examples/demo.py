"""Runnable demonstration of the agent-ready backend.

    uv run python examples/demo.py

Walks through the research scenario: import, loose reference resolution,
aggregate, materialize, describe, lineage, idempotent retry and structured
ambiguity handling. Uses a throwaway workspace so it can be re-run freely.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_backend import Backend  # noqa: E402

HERE = Path(__file__).resolve().parent


def show(title: str, response: dict, keys: list[str] | None = None) -> None:
    print(f"\n=== {title} ===")
    body = {k: response[k] for k in keys if k in response} if keys else response
    print(json.dumps(body, indent=2, default=str, ensure_ascii=False))


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="agent-backend-demo-"))
    backend = Backend(workspace)
    failures = 0
    try:
        # 1. Import orders.csv
        imported = backend.import_dataset(str(HERE / "orders.csv"), description="Orders exported from the shop")
        show("1. import_dataset(orders.csv)", imported, ["status", "operation_id", "dataset", "summary"])
        failures += imported["status"] != "success"

        # 2. Identify it from a loose reference
        described = backend.describe_dataset("the orders I imported today", sample_rows=2)
        show("2. describe_dataset('the orders I imported today')", described, ["status", "dataset", "hints", "resolution"])
        failures += described["status"] != "success"

        # 3. Group revenue by region (preview; nothing persisted)
        intent = {"group_by": ["region"], "metric": "revenue"}
        preview = backend.transform_dataset("the orders I imported today", intent)
        show("3. transform_dataset(group revenue by region)", preview, ["status", "operation_id", "result", "summary", "plan", "resolution"])
        failures += preview["status"] != "success"

        # 4. Materialize as regional_sales
        created = backend.materialize_result("the orders I imported today", intent, "regional_sales", description="Revenue by region")
        show("4. materialize_result(... 'regional_sales')", created, ["status", "operation_id", "dataset", "summary", "lineage", "plan"])
        failures += created["status"] != "success"

        # 5. Describe it
        described = backend.describe_dataset("regional_sales", sample_rows=0)
        show("5. describe_dataset('regional_sales')", described, ["status", "dataset", "schema", "row_count", "versions", "lineage"])
        failures += described["status"] != "success"

        # 6. Lineage / provenance
        provenance = backend.get_provenance("the result I created earlier")
        show("6. get_provenance('the result I created earlier')", provenance, ["status", "dataset", "produced_by", "inputs", "resolution"])
        failures += provenance["status"] != "success"

        # 7. Retry the same materialization: idempotent replay, no duplicate dataset
        retry = backend.materialize_result("the orders I imported today", intent, "regional_sales", description="Revenue by region")
        show("7. retry materialize_result (idempotent)", retry, ["status", "idempotent_replay", "operation_id", "dataset", "summary"])
        failures += not retry.get("idempotent_replay")
        listing = backend.list_datasets()
        print(f"\ndatasets after retry: {[d['name'] for d in listing['datasets']]} (count={listing['count']})")
        failures += listing["count"] != 2

        # 8. Ambiguous request → structured needs_resolution, then repair
        archive = workspace / "orders_archive.csv"
        shutil.copy(HERE / "orders.csv", archive)
        backend.import_dataset(str(archive), description="Archived copy of the orders")
        ambiguous = backend.transform_dataset("the orders data", intent)
        show("8a. transform_dataset('the orders data') → ambiguity", ambiguous, ["status", "code", "field", "message", "candidates", "hint"])
        failures += ambiguous["status"] != "needs_resolution"
        chosen = ambiguous["candidates"][0]["id"]
        repaired = backend.transform_dataset(chosen, intent)
        show(f"8b. repaired with candidate {chosen}", repaired, ["status", "source", "summary"])
        failures += repaired["status"] != "success"

        # Bonus: a typo in a column name is a structured, recoverable error
        typo = backend.transform_dataset("orders", {"group_by": ["regoin"], "metric": "revenue"})
        show("bonus: fuzzy column 'regoin'", typo, ["status", "summary", "resolution"])
        broken = backend.transform_dataset("orders", {"group_by": ["colour"], "metric": "revenue"})
        show("bonus: unknown column 'colour'", broken, ["status", "code", "message", "candidates"])

        report = backend.integrity_report()
        print(f"\nintegrity: {report}")
        failures += not report["ok"]
    finally:
        backend.close()
        shutil.rmtree(workspace, ignore_errors=True)
    print("\nDEMO OK" if not failures else f"\nDEMO FAILED ({failures} checks)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
