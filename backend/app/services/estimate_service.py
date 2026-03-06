"""Generate repair estimates from Solution nodes in the knowledge graph."""

from __future__ import annotations

from backend.app.db.neo4j_client import run_query
from backend.app.graph import queries
from backend.app.services.llm.client import chat
from backend.app.services.llm.prompts import ESTIMATE_SYSTEM

DEFAULT_LABOR_RATE = 100.0  # $/hr


def generate_estimate(solution_neo4j_id: str, labor_rate: float = DEFAULT_LABOR_RATE) -> dict:
    """Build a repair estimate from a Solution node and its children.

    Returns a dict with parts, tools, labor, and totals.
    """
    results = run_query(queries.SOLUTION_DETAILS, {"solution_id": solution_neo4j_id})

    if not results:
        return {"error": "Solution not found", "solution_id": solution_neo4j_id}

    solution = results[0].get("s", {})
    steps = []
    parts = {}
    tools = {}
    solution_parts = {}
    solution_tools = {}

    for row in results:
        # Collect steps
        step = row.get("step")
        if step and step.get("id"):
            step_id = step["id"]
            step_order = row.get("step_order", 0)
            if step_id not in {s["id"] for s in steps}:
                steps.append({**step, "step_order": step_order})

        # Collect parts from steps
        part = row.get("part")
        if part and part.get("id"):
            parts[part["id"]] = part

        # Collect tools from steps
        tool = row.get("tool")
        if tool and tool.get("id"):
            tools[tool["id"]] = tool

        # Collect parts/tools directly on solution
        s_part = row.get("sPart")
        if s_part and s_part.get("id"):
            solution_parts[s_part["id"]] = s_part

        s_tool = row.get("sTool")
        if s_tool and s_tool.get("id"):
            solution_tools[s_tool["id"]] = s_tool

    # Merge solution-level parts/tools
    parts.update(solution_parts)
    tools.update(solution_tools)

    # Sort steps by order
    steps.sort(key=lambda s: s.get("step_order", 0))

    # Calculate labor
    total_minutes = solution.get("total_labor_minutes", 0) or 0
    if not total_minutes and steps:
        total_minutes = sum(s.get("est_minutes", 0) or 0 for s in steps)

    labor_cost = (total_minutes / 60) * labor_rate

    # Calculate parts cost
    parts_list = []
    total_parts_low = 0.0
    total_parts_high = 0.0

    for part in parts.values():
        price = part.get("estimated_retail_price", 0) or 0
        name = part.get("name", "Unknown part")
        parts_list.append({
            "name": name,
            "oem_part_number": part.get("oem_part_number", ""),
            "estimated_price": price,
            "aftermarket": part.get("aftermarket", False),
        })
        total_parts_low += price * 0.8  # 20% range
        total_parts_high += price * 1.2

    tools_list = [
        {
            "name": t.get("name", "Unknown tool"),
            "category": t.get("category", ""),
            "common": t.get("common", True),
        }
        for t in tools.values()
    ]

    estimate = {
        "solution_title": solution.get("title", ""),
        "difficulty": solution.get("difficulty", "unknown"),
        "precautions": solution.get("precautions", ""),
        "steps": [
            {
                "order": s.get("step_order", 0),
                "title": s.get("title", ""),
                "instruction": s.get("instruction", ""),
                "est_minutes": s.get("est_minutes", 0),
                "warning": s.get("warning", ""),
            }
            for s in steps
        ],
        "parts": parts_list,
        "tools": tools_list,
        "labor_minutes": total_minutes,
        "labor_rate": labor_rate,
        "labor_cost": round(labor_cost, 2),
        "total_parts_low": round(total_parts_low, 2),
        "total_parts_high": round(total_parts_high, 2),
        "total_low": round(total_parts_low + labor_cost, 2),
        "total_high": round(total_parts_high + labor_cost, 2),
    }

    return estimate


def format_estimate(estimate: dict) -> str:
    """Use LLM to format the estimate into a readable summary."""
    import json
    raw = json.dumps(estimate, indent=2)

    response = chat(
        system=ESTIMATE_SYSTEM,
        messages=[{"role": "user", "content": f"Format this repair estimate:\n\n{raw}"}],
    )
    return response
