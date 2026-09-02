"""Scripted agents, one per task: the intents a real agent would issue, as MCP tool calls.

No model is involved. Each step receives the trace so far and returns
``(tool, arguments)``; arguments may be derived from earlier responses exactly
as a real agent would read them. The declaration is made first, as the
contract protocol asks (MADR 0007), so every export below is held to it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

Step = Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]]


def _last(trace: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    return next(t["response"] for t in reversed(trace) if t["tool"] == tool)


def _cell(trace: list[dict[str, Any]], tool: str, column: str, row: int = 0) -> Any:
    """One value out of a preview, the way an agent reads its own result."""
    result = _last(trace, tool)["result"]
    index = [c["name"] for c in result["columns"]].index(column)
    return result["rows"][row][index]


def task_10_script(context_dir: Path, prediction_path: Path) -> list[Step]:
    """按报告期从早到晚列出货币当局资产负债表中总资产金额非空的记录，返回报告期和总资产金额（单位：亿元）。"""
    return [
        lambda trace: ("declare_output", {"columns": ["EndDate", "TotalAssets"], "rows": "at_least_one",
                                          "description": "reporting period and total assets, oldest first"}),
        lambda trace: ("import_workspace", {"path": str(context_dir)}),
        lambda trace: ("attach_metadata", {"source": "knowledge.md"}),
        lambda trace: ("search_datasets", {"query": "monetary authority balance sheet total assets"}),
        lambda trace: ("describe_dataset", {"dataset": _last(trace, "search_datasets")["results"][0]["name"], "sample_rows": 2}),
        lambda trace: ("materialize_result", {
            "source": _last(trace, "search_datasets")["results"][0]["name"],
            "transform": {
                "filter": {"field": "total assets", "op": "not_null"},
                "select": ["enddate", "total assets"],
                "sort": "enddate",
            },
            "name": "task_10_answer",
            "description": "Monetary authority balance sheet records with non-null total assets, by reporting period",
        }),
        # The question prescribes the rendering: at most 4 decimals, no trailing
        # zeros, whole amounts keep one decimal. That is an export rule (MADR 0005).
        lambda trace: ("export_result", {
            "dataset": "task_10_answer", "path": str(prediction_path), "overwrite": True,
            "format_spec": {"decimals": 4, "strip_trailing_zeros": True, "integer_min_decimals": 1},
        }),
    ]


def task_44_script(context_dir: Path, prediction_path: Path) -> list[Step]:
    """What procedures did patient 025-44842 receive at the latest treatment timestamp, by treatment id?"""
    # The patient is only named in the cost table; treatments are linked through
    # cost.eventid, so every step below runs on the joined relation.
    linked = [
        {"join": {"right": "cost", "on": {"treatmentid": "eventid"}}},
        {"filter": "uniquepid = '025-44842' and eventtype = 'treatment'"},
    ]
    return [
        lambda trace: ("declare_output", {"columns": ["treatmentname"], "rows": "at_least_one",
                                          "description": "procedures at the latest treatment timestamp, in treatment id order"}),
        lambda trace: ("import_workspace", {"path": str(context_dir)}),
        lambda trace: ("describe_dataset", {"dataset": "treatment", "sample_rows": 2}),
        lambda trace: ("transform_dataset", {
            "source": "treatment",
            "transform": linked + [{"sort": "-treatmenttime"}, {"limit": 1}],
        }),
        lambda trace: ("materialize_result", {
            "source": "treatment",
            "transform": linked + [
                {"filter": {"field": "treatmenttime", "op": "=", "value": _cell(trace, "transform_dataset", "treatmenttime")}},
                {"sort": "treatmentid"},
                {"select": ["treatmentname"]},
            ],
            "name": "task_44_answer",
            "description": "Procedures at the patient's latest treatment timestamp, in treatment id order",
        }),
        lambda trace: ("export_result", {"dataset": "task_44_answer", "path": str(prediction_path), "overwrite": True}),
    ]


def task_127_script(context_dir: Path, prediction_path: Path) -> list[Step]:
    """What was the maximum recorded respiration value for patient 027-146876 on 2103-07-12?"""
    return [
        lambda trace: ("declare_output", {"columns": ["maximum_respiration"], "rows": "one"}),
        lambda trace: ("import_workspace", {"path": str(context_dir)}),
        lambda trace: ("describe_dataset", {"dataset": "vitalperiodic", "sample_rows": 2}),
        lambda trace: ("transform_dataset", {
            "source": "patient",
            "transform": {"filter": "uniquepid = '027-146876'", "select": ["patientunitstayid"]},
        }),
        lambda trace: ("materialize_result", {
            "source": "vitalperiodic",
            "transform": [
                {"filter": f"patientunitstayid = {_cell(trace, 'transform_dataset', 'patientunitstayid')} "
                           f"and strftime(observationtime, '%Y-%m-%d') = '2103-07-12'"},
                {"aggregate": [{"function": "max", "field": "respiration", "alias": "maximum_respiration"}]},
            ],
            "name": "task_127_answer",
            "description": "Maximum respiration recorded for the patient on 2103-07-12",
        }),
        lambda trace: ("export_result", {"dataset": "task_127_answer", "path": str(prediction_path), "overwrite": True}),
    ]


def task_329_script(context_dir: Path, prediction_path: Path) -> list[Step]:
    """Daily maximum enteral formula volume/bolus amt (ml) for patient 033-22108 in the current encounter."""
    label = "enteral formula volume/bolus amt (ml)"
    return [
        lambda trace: ("declare_output", {"columns": ["daily_maximum"], "rows": "one"}),
        lambda trace: ("import_workspace", {"path": str(context_dir)}),
        lambda trace: ("transform_dataset", {
            "source": "patient",
            "transform": {"filter": "uniquepid = '033-22108'", "select": ["patientunitstayid"]},
        }),
        # Two managed datasets: the daily totals, then the maximum over them.
        lambda trace: ("materialize_result", {
            "source": "intakeoutput",
            "transform": [
                {"filter": f"patientunitstayid = {_cell(trace, 'transform_dataset', 'patientunitstayid')} "
                           f"and celllabel = '{label}'"},
                {"aggregate": {"group_by": ["date(intakeoutputtime) as day"],
                               "measures": [{"function": "sum", "field": "cellvaluenumeric", "alias": "daily_total"}]}},
            ],
            "name": "task_329_daily_totals",
            "description": "Enteral formula volume per calendar day for the patient",
        }),
        lambda trace: ("materialize_result", {
            "source": "task_329_daily_totals",
            "transform": {"aggregate": [{"function": "max", "field": "daily_total", "alias": "daily_maximum"}]},
            "name": "task_329_answer",
            "description": "Largest daily enteral formula volume for the patient",
        }),
        lambda trace: ("export_result", {"dataset": "task_329_answer", "path": str(prediction_path), "overwrite": True}),
    ]


SCRIPTS: dict[str, Callable[[Path, Path], list[Step]]] = {
    "task_10": task_10_script,
    "task_44": task_44_script,
    "task_127": task_127_script,
    "task_329": task_329_script,
}


async def run_script(server, steps: list[Step]) -> list[dict[str, Any]]:
    """Issue the steps against an MCP server, stopping at the first response the script cannot repair."""
    trace: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        tool, arguments = step(trace)
        started = time.perf_counter()
        result = await server.call_tool(tool, arguments)
        response = result.structured_content or {}
        entry = {
            "step": index,
            "tool": tool,
            "arguments": arguments,
            "status": response.get("status"),
            "code": response.get("code"),
            "summary": response.get("summary") or response.get("message"),
            "resolution": response.get("resolution"),
            "elapsed_s": round(time.perf_counter() - started, 3),
            "response": response,
        }
        trace.append(entry)
        print(f"[{index}] {tool} → {entry['status']}" + (f" ({entry['code']})" if entry["code"] else "") + f": {entry['summary']}")
        if entry["status"] not in ("success", "partial"):
            print("    stopping: the scripted agent has no repair strategy for this response")
            break
    return trace
