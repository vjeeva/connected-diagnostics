"""Technician knowledge rules service.

Stores and retrieves tech-contributed corrections and requirements that get
injected into LLM prompts. These are not shop-specific preferences — they're
universal knowledge corrections (things the system missed or got wrong).

Examples:
  - "Valve body work always requires full ATF drain and refill"
  - "Always replace oil pan gasket when dropping the pan — never reuse"
  - "P2714 solenoid SLT is accessible without removing full valve body on GX460"
"""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine, text

from backend.app.core.config import settings

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.postgres_sync_url, pool_size=3, pool_pre_ping=True)
    return _engine


def save_rule(
    rule_text: str,
    category: str = "work_order",
    scope: str = "global",
    scope_value: str | None = None,
    contributed_by: str | None = None,
    source_session: str | None = None,
    status: str = "active",
) -> str:
    """Save a shop rule. Returns the rule ID."""
    engine = _get_engine()
    rule_id = str(uuid.uuid4())
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO shop_rules (id, rule_text, category, scope, scope_value, contributed_by, source_session, status)
                VALUES (:id, :rule, :cat, :scope, :sv, :by, :sess, :status)
            """),
            {
                "id": rule_id, "rule": rule_text, "cat": category,
                "scope": scope, "sv": scope_value,
                "by": contributed_by, "sess": source_session, "status": status,
            },
        )
        conn.commit()
    return rule_id


def get_rules(
    category: str | None = None,
    scope: str | None = None,
    scope_value: str | None = None,
) -> list[dict]:
    """Get active shop rules, optionally filtered."""
    engine = _get_engine()
    where = "status = 'active'"
    params: dict = {}
    if category:
        where += " AND category = :cat"
        params["cat"] = category
    if scope:
        where += " AND scope = :scope"
        params["scope"] = scope
    if scope_value:
        where += " AND (scope_value = :sv OR scope_value IS NULL)"
        params["sv"] = scope_value

    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT id, rule_text, category, scope, scope_value, contributed_by, created_at FROM shop_rules WHERE {where} ORDER BY created_at"),
            params,
        ).fetchall()

    return [
        {
            "id": str(row[0]), "rule_text": row[1], "category": row[2],
            "scope": row[3], "scope_value": row[4],
            "contributed_by": row[5], "created_at": row[6],
        }
        for row in rows
    ]


def get_rules_for_prompt() -> str:
    """Load all active rules and format for LLM prompt injection.

    Loads every active rule — they're short and the LLM determines
    which apply to the current context.
    Returns empty string if no rules exist.
    """
    rules = get_rules()
    if not rules:
        return ""

    lines = ["TECHNICIAN CORRECTIONS (verified knowledge from experienced techs — always follow these):"]
    for r in rules:
        lines.append(f"  • {r['rule_text']}")

    return "\n".join(lines)


def disable_rule(rule_id: str) -> bool:
    """Disable a rule. Returns True if found and disabled."""
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("UPDATE shop_rules SET status = 'disabled' WHERE id = :id AND status = 'active'"),
            {"id": rule_id},
        )
        conn.commit()
    return result.rowcount > 0
