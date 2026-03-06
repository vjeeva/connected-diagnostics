"""Track QA run metrics over time for improvement visibility.

Stores run history in a JSON file at the project root. Each entry records
page range, metrics, cost estimates, and fixes applied so you can see
how quality improves across iterations.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from backend.app.qa.analyzer import QAReport

TRACKER_FILE = Path(__file__).resolve().parents[4] / "qa_history.json"


def _load_history() -> list[dict]:
    if TRACKER_FILE.exists():
        return json.loads(TRACKER_FILE.read_text())
    return []


def _save_history(history: list[dict]):
    TRACKER_FILE.write_text(json.dumps(history, indent=2, default=str))


def log_run(
    report: QAReport,
    run_type: str = "analyze",
    cost_estimate_usd: float | None = None,
    fixes_applied: list[str] | None = None,
    notes: str = "",
) -> dict:
    """Log a QA run to the tracker file. Returns the logged entry."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_type": run_type,
        "page_range": list(report.page_range),
        "summary": report.summary,
        "issue_counts": {
            "errors": report.error_count,
            "warnings": report.warning_count,
            "total": len(report.issues),
        },
        "issues_by_check": _group_issues(report.issues),
        "trees": [
            {
                "title": t.problem_title,
                "dtc": t.dtc_codes,
                "depth": t.max_depth,
                "nodes": t.node_count,
                "leaf_types": t.leaf_types,
            }
            for t in report.trees
        ],
        "cost_estimate_usd": cost_estimate_usd,
        "fixes_applied": fixes_applied or [],
        "notes": notes,
    }

    history = _load_history()
    history.append(entry)
    _save_history(history)
    return entry


def get_history(page_range: tuple[int, int] | None = None) -> list[dict]:
    """Get all QA run history, optionally filtered by page range."""
    history = _load_history()
    if page_range:
        history = [
            h for h in history
            if h["page_range"] == list(page_range)
        ]
    return history


def compare_last_two(page_range: tuple[int, int] | None = None) -> dict | None:
    """Compare the last two runs for a page range. Returns delta dict or None."""
    history = get_history(page_range)
    if len(history) < 2:
        return None

    prev, curr = history[-2], history[-1]
    prev_s, curr_s = prev["summary"], curr["summary"]

    delta = {}
    for key in ["total_nodes", "total_relationships", "tree_count", "avg_depth", "max_depth", "errors", "warnings"]:
        old_val = prev_s.get(key, 0)
        new_val = curr_s.get(key, 0)
        delta[key] = {"old": old_val, "new": new_val, "change": new_val - old_val}

    delta["total_cost_usd"] = sum(h.get("cost_estimate_usd", 0) or 0 for h in history)
    delta["run_count"] = len(history)

    return delta


def _group_issues(issues) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.check] = counts.get(issue.check, 0) + 1
    return counts
