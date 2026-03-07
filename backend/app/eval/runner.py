"""Eval runner — drives test cases through the diagnostic engine and evaluates results."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from backend.app.core.config import settings
from backend.app.services.diagnostic_engine import continue_session, start_session
from backend.app.services.llm.client import interpret

CASES_PATH = Path(__file__).parent / "test_cases.yaml"
RESULTS_PATH = Path(__file__).resolve().parents[3] / "eval_results.json"


@dataclass
class CheckResult:
    check_type: str
    passed: bool
    message: str
    evidence: str = ""
    fix_hint: str = ""


@dataclass
class CaseResult:
    test_id: str
    name: str
    status: str = ""  # PASS / FAIL / ERROR
    turns: list[str] = field(default_factory=list)
    final_output: str = ""
    checks: list[dict] = field(default_factory=list)
    error: str = ""
    duration_s: float = 0.0


def load_cases(case_id: str | None = None, tag: str | None = None) -> list[dict]:
    """Load test cases from YAML, optionally filtered."""
    with open(CASES_PATH) as f:
        cases = yaml.safe_load(f)
    if case_id:
        cases = [c for c in cases if c["id"] == case_id]
    if tag:
        cases = [c for c in cases if tag in c.get("tags", [])]
    return cases


def _run_turns(turns: list[str]) -> str:
    """Run a conversation through the engine, return the final response."""
    state, response = start_session(turns[0])
    for turn in turns[1:]:
        state, response = continue_session(state, turn)
    return response


def _check_regex_present(output: str, check: dict) -> CheckResult:
    pattern = check["pattern"]
    match = re.search(pattern, output)
    if match:
        return CheckResult("regex_present", True, check["message"])
    return CheckResult(
        "regex_present", False, check["message"],
        evidence=f"Pattern not found: {pattern}",
        fix_hint=_hint_for_missing_content(check["message"]),
    )


def _check_regex_absent(output: str, check: dict) -> CheckResult:
    pattern = check["pattern"]
    match = re.search(pattern, output)
    if not match:
        return CheckResult("regex_absent", True, check["message"])
    # Find the line containing the match for evidence
    for line in output.split("\n"):
        if re.search(pattern, line):
            return CheckResult(
                "regex_absent", False, check["message"],
                evidence=f"Found: {line.strip()[:200]}",
                fix_hint="parts_catalog.py (lookup missed), diagnostic_engine.py (component extraction), or scrape_parts.py (part not in DB)",
            )
    return CheckResult("regex_absent", False, check["message"], evidence=f"Pattern matched: {pattern}")


def _check_parts_present(output: str, check: dict) -> CheckResult:
    missing = [pn for pn in check["parts"] if pn not in output]
    if not missing:
        return CheckResult("parts_present", True, check["message"])
    return CheckResult(
        "parts_present", False, check["message"],
        evidence=f"Missing part numbers: {', '.join(missing)}",
        fix_hint="Check: 1) Is the part in parts_catalog table? 2) Does _extract_component_names find it? 3) Does get_parts_for_work_order include it? Files: parts_catalog.py, diagnostic_engine.py",
    )


def _check_min_lines(output: str, check: dict) -> CheckResult:
    count = len([l for l in output.split("\n") if l.strip()])
    threshold = check["count"]
    if count >= threshold:
        return CheckResult("min_lines", True, check["message"])
    return CheckResult(
        "min_lines", False, check["message"],
        evidence=f"Response has {count} non-empty lines, expected >= {threshold}",
        fix_hint="LLM response too short. Check: model routing (strong vs light), system prompt, or context injection in diagnostic_engine.py",
    )


def _check_llm_judge(output: str, check: dict) -> CheckResult:
    """Use an LLM to evaluate nuanced criteria."""
    criteria = check["criteria"]
    prompt = (
        f"Evaluate this diagnostic chat output against the following criteria.\n\n"
        f"CRITERIA: {criteria}\n\n"
        f"OUTPUT TO EVALUATE:\n{output[:6000]}\n\n"
        f"Does the output meet ALL the criteria? Respond with EXACTLY this JSON:\n"
        f'{{"pass": true/false, "reason": "brief explanation"}}'
    )
    try:
        raw = interpret(
            system="You are a QA evaluator for an automotive diagnostic system. Evaluate strictly — if any part of the criteria is not met, fail it.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
        )
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)
        result = json.loads(cleaned)
        passed = result.get("pass", False)
        reason = result.get("reason", "")
        return CheckResult(
            "llm_judge", passed, check["message"],
            evidence=reason if not passed else "",
            fix_hint="prompts.py (system prompt rules), diagnostic_engine.py (context/search queries)" if not passed else "",
        )
    except Exception as e:
        return CheckResult(
            "llm_judge", False, check["message"],
            evidence=f"LLM judge error: {e}",
            fix_hint="LLM judge call failed — check API key and model config",
        )


_CHECK_DISPATCH = {
    "regex_present": _check_regex_present,
    "regex_absent": _check_regex_absent,
    "parts_present": _check_parts_present,
    "min_lines": _check_min_lines,
    "llm_judge": _check_llm_judge,
}


def _hint_for_missing_content(message: str) -> str:
    """Map failure messages to likely code areas."""
    msg_lower = message.lower()
    if "part" in msg_lower or "oem" in msg_lower or "price" in msg_lower:
        return "parts_catalog.py (lookup/formatting), diagnostic_engine.py (component extraction or parts injection), scrape_parts.py (part not scraped)"
    if "atf" in msg_lower or "drain" in msg_lower or "refill" in msg_lower or "fluid" in msg_lower:
        return "prompts.py (FLUIDS AND CONSUMABLES rules), shop_rules table (technician corrections), diagnostic_engine.py (tech rules injection)"
    if "gasket" in msg_lower or "seal" in msg_lower or "o-ring" in msg_lower:
        return "prompts.py (GASKETS AND SEALS rules), parts_catalog.py (category-based expansion)"
    if "torque" in msg_lower or "spec" in msg_lower:
        return "diagnostic_engine.py (chunk search queries — need 'torque specification' search), prompts.py (work order rules)"
    if "labor" in msg_lower or "time" in msg_lower or "hour" in msg_lower:
        return "prompts.py (work order format rules, labor time guidance)"
    if "format" in msg_lower or "header" in msg_lower or "section" in msg_lower:
        return "prompts.py (WORK ORDER FORMAT template)"
    if "graph" in msg_lower or "no info" in msg_lower:
        return "diagnostic_engine.py (find_matching_problems, chunk search), neo4j data coverage"
    return "diagnostic_engine.py, prompts.py"


def _classify_failure(cr: CheckResult) -> str:
    """Classify a failure as 'code' or 'data' to guide the fix loop."""
    hint = cr.fix_hint.lower()
    evidence = cr.evidence.lower()
    # Data gaps: part not in DB, manual section not ingested
    if "not scraped" in hint or "not in db" in evidence or "not in parts_catalog" in evidence:
        return "data"
    if "neo4j data coverage" in hint:
        return "data"
    return "code"


def run_case(case: dict) -> CaseResult:
    """Run a single test case and return results."""
    t0 = time.time()
    result = CaseResult(test_id=case["id"], name=case["name"], turns=case["turns"])
    try:
        output = _run_turns(case["turns"])
        result.final_output = output
    except Exception as e:
        result.status = "ERROR"
        result.error = str(e)
        result.duration_s = time.time() - t0
        return result

    # Run checks
    all_passed = True
    for check in case.get("checks", []):
        check_type = check["type"]
        handler = _CHECK_DISPATCH.get(check_type)
        if not handler:
            cr = CheckResult(check_type, False, f"Unknown check type: {check_type}")
        else:
            cr = handler(output, check)
        if not cr.passed:
            all_passed = False
            cr_dict = asdict(cr)
            cr_dict["failure_type"] = _classify_failure(cr)
        else:
            cr_dict = asdict(cr)
        result.checks.append(cr_dict)

    result.status = "PASS" if all_passed else "FAIL"
    result.duration_s = time.time() - t0
    return result


def run_all(case_id: str | None = None, tag: str | None = None) -> list[CaseResult]:
    """Run all matching test cases and return results."""
    cases = load_cases(case_id=case_id, tag=tag)
    results = []
    # Group cases by identical turns to avoid duplicate LLM calls
    turn_groups: dict[str, list[dict]] = {}
    for case in cases:
        key = json.dumps(case["turns"])
        turn_groups.setdefault(key, []).append(case)

    for turns_key, group in turn_groups.items():
        turns = json.loads(turns_key)
        # Run the conversation once
        t0 = time.time()
        try:
            output = _run_turns(turns)
            elapsed = time.time() - t0
        except Exception as e:
            elapsed = time.time() - t0
            for case in group:
                r = CaseResult(test_id=case["id"], name=case["name"], turns=turns,
                               status="ERROR", error=str(e), duration_s=elapsed)
                results.append(r)
            continue

        # Evaluate each case's checks against the same output
        for case in group:
            result = CaseResult(test_id=case["id"], name=case["name"], turns=turns,
                                final_output=output, duration_s=elapsed)
            all_passed = True
            for check in case.get("checks", []):
                handler = _CHECK_DISPATCH.get(check["type"])
                if not handler:
                    cr = CheckResult(check["type"], False, f"Unknown check type: {check['type']}")
                else:
                    cr = handler(output, check)
                cr_dict = asdict(cr)
                if not cr.passed:
                    all_passed = False
                    cr_dict["failure_type"] = _classify_failure(cr)
                result.checks.append(cr_dict)
            result.status = "PASS" if all_passed else "FAIL"
            results.append(result)

    return results


def save_results(results: list[CaseResult], path: Path | None = None):
    """Save results to JSON for the fix loop to consume."""
    out = path or RESULTS_PATH
    data = [asdict(r) for r in results]
    with open(out, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return out
