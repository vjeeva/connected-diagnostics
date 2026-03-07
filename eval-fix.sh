#!/bin/bash
# Automated eval → fix loop.
#
# Runs the eval suite, and if any tests fail, feeds the results to Claude Code
# headless mode to fix the code. Repeats up to MAX_ITERATIONS times.
#
# Usage:
#   bash eval-fix.sh              # run all tests
#   bash eval-fix.sh --tag work_order   # filter by tag
#   bash eval-fix.sh --case p2714_work_order_parts  # single test

set -euo pipefail

PYTHON="${HOME}/.pyenv/versions/connected-diagnostics/bin/python"
MAX_ITERATIONS=${MAX_ITERATIONS:-3}
EVAL_ARGS="${@}"

echo "=== Eval-Fix Loop (max ${MAX_ITERATIONS} iterations) ==="
echo ""

for i in $(seq 1 $MAX_ITERATIONS); do
    echo "━━━ Iteration $i/$MAX_ITERATIONS ━━━"
    echo ""

    # Run eval — exits 0 if all pass, 1 if any fail
    if $PYTHON -m backend.cli.eval run $EVAL_ARGS; then
        echo ""
        echo "All tests pass!"
        exit 0
    fi

    echo ""

    # Check if there are code failures (not just data gaps)
    CODE_FAILURES=$($PYTHON -c "
import json, sys
results = json.load(open('eval_results.json'))
code_fails = []
for r in results:
    if r['status'] != 'FAIL':
        continue
    for c in r['checks']:
        if not c['passed'] and c.get('failure_type', 'code') == 'code':
            code_fails.append({'test': r['test_id'], 'message': c['message']})
print(len(code_fails))
" 2>/dev/null || echo "0")

    if [ "$CODE_FAILURES" = "0" ]; then
        echo "Only data gaps remaining — no code fixes possible."
        echo "Data gaps require scraping missing parts or ingesting more manual pages."
        exit 1
    fi

    echo "Found $CODE_FAILURES code failure(s). Invoking Claude Code to fix..."
    echo ""

    # Feed failures to Claude Code in headless mode
    claude -p --dangerously-skip-permissions "You are fixing failing eval tests for the connected-diagnostics project.

Read eval_results.json in the project root. It contains test results with:
- test_id, name: which test failed
- final_output: the full LLM response that was generated
- checks[]: each check with passed/message/evidence/fix_hint/failure_type

Fix ONLY checks where failure_type='code' (skip failure_type='data' — those need scraping, not code).

For each failure:
1. Read the fix_hint to find the right file
2. Read that file to understand the current code
3. Make the minimal change to fix the failure
4. Do NOT break other tests — changes should be additive

The main files you'll work with:
- backend/app/services/diagnostic_engine.py — search queries, context injection, model routing
- backend/app/services/parts_catalog.py — parts lookup and formatting
- backend/app/services/llm/prompts.py — system prompts and work order rules
- backend/app/services/shop_rules.py — technician corrections

After making changes, briefly explain what you fixed and why."

    echo ""
    echo "Claude Code finished. Re-running eval..."
    echo ""
done

echo ""
echo "Still failing after $MAX_ITERATIONS iterations."
echo "Review eval_results.json for remaining issues."
exit 1
