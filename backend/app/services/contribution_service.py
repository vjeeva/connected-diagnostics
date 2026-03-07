"""Technician contribution system.

Handles creating contributions, routing through trust-based approval,
and publishing to the graph.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from backend.app.core.config import settings
from backend.app.db.neo4j_client import get_driver
from backend.app.graph.mutations import create_node, create_relationship, _new_id


def _engine():
    return create_engine(settings.postgres_sync_url)


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def create_user(
    email: str,
    display_name: str,
    password_hash: str,
    user_type: str = "technician",
    trust_level: str = "standard",
    trust_source: str = "earned",
) -> str:
    """Create a user. Returns the user ID."""
    user_id = str(uuid.uuid4())
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO users (id, email, display_name, password_hash, user_type, trust_level, trust_source)
            VALUES (:id, :email, :name, :pw, :utype, :tlevel, :tsource)
        """), {
            "id": user_id, "email": email, "name": display_name,
            "pw": password_hash, "utype": user_type,
            "tlevel": trust_level, "tsource": trust_source,
        })
        conn.commit()
    engine.dispose()
    return user_id


def invite_technician(email: str, display_name: str) -> str:
    """Invite a technician with immediate Trusted status (bootstrap mode)."""
    return create_user(
        email=email,
        display_name=display_name,
        password_hash="placeholder",  # real auth comes with the web app
        user_type="technician",
        trust_level="trusted",
        trust_source="invited",
    )


def get_user(user_id: str) -> dict | None:
    engine = _engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM users WHERE id = :id"),
            {"id": user_id},
        ).mappings().first()
    engine.dispose()
    return dict(row) if row else None


def update_reputation(user_id: str, delta: int) -> int:
    """Add delta to user's reputation. Returns new total."""
    engine = _engine()
    with engine.connect() as conn:
        result = conn.execute(text("""
            UPDATE users SET reputation = reputation + :delta
            WHERE id = :id RETURNING reputation
        """), {"id": user_id, "delta": delta})
        new_rep = result.scalar()
        conn.commit()
    engine.dispose()
    return new_rep


# ---------------------------------------------------------------------------
# Contribution creation
# ---------------------------------------------------------------------------

VALID_CONTRIBUTION_TYPES = {"new_node", "alternative", "annotation", "attachment", "cost_update", "shop_rule"}


def submit_contribution(
    user_id: str,
    contribution_type: str,
    target_node_id: str | None,
    content: dict,
) -> dict:
    """Submit a contribution. Routes through trust model.

    Returns dict with contribution_id, status, and message.
    """
    if contribution_type not in VALID_CONTRIBUTION_TYPES:
        raise ValueError(f"Invalid contribution type: {contribution_type}")

    user = get_user(user_id)
    if not user:
        raise ValueError("User not found")

    trust_level = user["trust_level"]
    trust_mode = settings.trust_mode

    # Determine if this publishes directly or goes to review
    if trust_mode == "bootstrap":
        if trust_level in ("trusted", "expert", "admin"):
            status = "published"
        else:
            raise PermissionError("Bootstrap mode is invite-only. Contact admin for access.")
    elif trust_mode == "hybrid":
        if trust_level in ("trusted", "expert", "admin"):
            status = "published"
        else:
            status = "pending_review"
    else:  # reputation
        if trust_level in ("expert", "admin"):
            status = "published"
        elif trust_level == "trusted":
            # Annotations, cost updates, and shop rules publish directly for trusted
            if contribution_type in ("annotation", "cost_update", "attachment", "shop_rule"):
                status = "published"
            else:
                status = "pending_review"
        else:
            status = "pending_review"

    contribution_id = str(uuid.uuid4())

    # Write contribution record to Postgres
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO contributions (id, user_id, contribution_type, target_neo4j_node_id, content, status)
            VALUES (:id, :uid, :ctype, :target, CAST(:content AS jsonb), :status)
        """), {
            "id": contribution_id,
            "uid": user_id,
            "ctype": contribution_type,
            "target": target_node_id,
            "content": __import__("json").dumps(content),
            "status": status,
        })
        conn.commit()
    engine.dispose()

    # If published immediately, apply to graph
    neo4j_node_id = None
    if status == "published":
        neo4j_node_id = _apply_contribution(contribution_id, contribution_type, target_node_id, content, user)

    return {
        "contribution_id": contribution_id,
        "status": status,
        "neo4j_node_id": neo4j_node_id,
        "message": _status_message(status, trust_mode),
    }


def _status_message(status: str, trust_mode: str) -> str:
    if status == "published":
        return "Published. Other technicians can now see your contribution."
    elif trust_mode == "hybrid":
        return "Submitted for review. Two trusted users need to approve."
    else:
        return "Submitted for review."


# ---------------------------------------------------------------------------
# Applying contributions to the graph
# ---------------------------------------------------------------------------

def _apply_contribution(
    contribution_id: str,
    contribution_type: str,
    target_node_id: str | None,
    content: dict,
    user: dict,
) -> str | None:
    """Write the contribution to Neo4j. Returns created node ID if applicable."""
    if contribution_type == "annotation":
        return _apply_annotation(target_node_id, content, user)
    elif contribution_type == "alternative":
        return _apply_alternative(target_node_id, content, user)
    elif contribution_type == "new_node":
        return _apply_new_node(target_node_id, content, user)
    elif contribution_type == "cost_update":
        return _apply_cost_update(target_node_id, content, user)
    elif contribution_type == "shop_rule":
        return _apply_shop_rule(content, user)
    return None


def _apply_annotation(target_node_id: str, content: dict, user: dict) -> None:
    """Add an annotation to an existing node.

    Annotations are stored as properties on the node:
    - annotations: list of {text, author, created_at}
    """
    driver = get_driver()
    annotation = {
        "text": content.get("text", ""),
        "author": user["display_name"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    with driver.session() as s:
        # Append to annotations list (stored as JSON string)
        s.run("""
            MATCH (n {id: $node_id})
            SET n.annotations = CASE
                WHEN n.annotations IS NULL THEN $new_list
                ELSE n.annotations + $annotation
            END
        """, {
            "node_id": target_node_id,
            "new_list": [__import__("json").dumps(annotation)],
            "annotation": __import__("json").dumps(annotation),
        })
    return None


def _apply_alternative(target_node_id: str, content: dict, user: dict) -> str:
    """Add an alternative test/step alongside an existing one.

    Creates a new node and links it with an ALTERNATIVE edge.
    """
    node_type = content.get("node_type", "Test")
    props = {
        "title": content.get("title", ""),
        "instruction": content.get("instruction", ""),
        "source_type": "technician",
        "contributed_by": user["display_name"],
    }
    if content.get("expected_result"):
        props["expected_result"] = content["expected_result"]
    if content.get("tool_required"):
        props["tool_required"] = content["tool_required"]

    node_id = create_node(node_type, props)

    # Link with ALTERNATIVE edge to the original
    create_relationship(node_id, target_node_id, "ALTERNATIVE", {
        "vote_score": 0,
        "contributed_by": user["display_name"],
    })

    # Update contribution record with created node ID
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE contributions SET created_neo4j_node_id = :nid
            WHERE id = (
                SELECT id FROM contributions
                WHERE target_neo4j_node_id = :target AND user_id = :uid
                ORDER BY created_at DESC LIMIT 1
            )
        """), {"nid": node_id, "target": target_node_id, "uid": str(user["id"])})
        conn.commit()
    engine.dispose()

    return node_id


def _apply_new_node(target_node_id: str | None, content: dict, user: dict) -> str:
    """Add a new node to the graph, optionally linked to a target."""
    node_type = content.get("node_type", "Step")
    props = {
        "title": content.get("title", ""),
        "instruction": content.get("instruction", ""),
        "source_type": "technician",
        "contributed_by": user["display_name"],
    }
    for key in ("expected_result", "tool_required", "description"):
        if content.get(key):
            props[key] = content[key]

    node_id = create_node(node_type, props)

    if target_node_id:
        rel_type = content.get("rel_type", "NEXT_STEP")
        create_relationship(target_node_id, node_id, rel_type)

    return node_id


def _apply_cost_update(target_node_id: str, content: dict, user: dict) -> None:
    """Update cost/time estimates on a Solution or Step node."""
    driver = get_driver()
    updates = {}
    if "labor_minutes" in content:
        updates["total_labor_minutes"] = content["labor_minutes"]
    if "difficulty" in content:
        updates["difficulty"] = content["difficulty"]

    if updates:
        set_clause = ", ".join(f"n.{k} = ${k}" for k in updates)
        with driver.session() as s:
            s.run(
                f"MATCH (n {{id: $node_id}}) SET {set_clause}",
                {"node_id": target_node_id, **updates},
            )
    return None


def _apply_shop_rule(content: dict, user: dict) -> None:
    """Write a shop rule to the shop_rules table as active."""
    from backend.app.services.shop_rules import save_rule
    save_rule(
        rule_text=content.get("rule_text", ""),
        category=content.get("category", "work_order"),
        scope=content.get("scope", "global"),
        scope_value=content.get("scope_value"),
        contributed_by=user["display_name"],
        source_session=content.get("source_session"),
    )
    return None


# ---------------------------------------------------------------------------
# Review / approval
# ---------------------------------------------------------------------------

def review_contribution(
    contribution_id: str,
    reviewer_id: str,
    action: str,
    notes: str | None = None,
) -> dict:
    """Review a pending contribution. Actions: approve, reject, flag.

    In hybrid mode, 2 approvals = published.
    """
    if action not in ("approve", "reject", "flag"):
        raise ValueError(f"Invalid review action: {action}")

    reviewer = get_user(reviewer_id)
    if not reviewer or reviewer["trust_level"] not in ("trusted", "expert", "admin"):
        raise PermissionError("Only trusted+ users can review contributions.")

    engine = _engine()
    with engine.connect() as conn:
        # Record the review
        conn.execute(text("""
            INSERT INTO contribution_reviews (id, contribution_id, reviewer_id, action, notes)
            VALUES (:id, :cid, :rid, :action, :notes)
            ON CONFLICT (contribution_id, reviewer_id) DO UPDATE SET action = :action, notes = :notes
        """), {
            "id": str(uuid.uuid4()),
            "cid": contribution_id,
            "rid": reviewer_id,
            "action": action,
            "notes": notes,
        })

        if action == "reject":
            conn.execute(text("""
                UPDATE contributions SET status = 'rejected' WHERE id = :cid
            """), {"cid": contribution_id})
            # Get contributor to deduct rep
            row = conn.execute(text(
                "SELECT user_id FROM contributions WHERE id = :cid"
            ), {"cid": contribution_id}).first()
            if row:
                conn.execute(text(
                    "UPDATE users SET reputation = reputation - 10 WHERE id = :uid"
                ), {"uid": str(row[0])})
            conn.commit()
            engine.dispose()
            return {"status": "rejected", "message": "Contribution rejected."}

        if action == "flag":
            conn.commit()
            engine.dispose()
            return {"status": "flagged", "message": "Flagged for admin review."}

        # Count approvals
        result = conn.execute(text("""
            SELECT COUNT(*) FROM contribution_reviews
            WHERE contribution_id = :cid AND action = 'approve'
        """), {"cid": contribution_id})
        approval_count = result.scalar()

        threshold = 2 if settings.trust_mode == "hybrid" else 1

        if approval_count >= threshold:
            # Get contribution details for publishing
            contrib = conn.execute(text(
                "SELECT * FROM contributions WHERE id = :cid"
            ), {"cid": contribution_id}).mappings().first()

            if contrib and contrib["status"] == "pending_review":
                conn.execute(text(
                    "UPDATE contributions SET status = 'published' WHERE id = :cid"
                ), {"cid": contribution_id})

                # Award rep to contributor
                conn.execute(text(
                    "UPDATE users SET reputation = reputation + 10 WHERE id = :uid"
                ), {"uid": str(contrib["user_id"])})

                conn.commit()
                engine.dispose()

                # Apply to graph
                import json
                content = json.loads(contrib["content"]) if isinstance(contrib["content"], str) else contrib["content"]
                contributor = get_user(str(contrib["user_id"]))
                _apply_contribution(
                    contribution_id,
                    contrib["contribution_type"],
                    contrib["target_neo4j_node_id"],
                    content,
                    contributor,
                )

                return {"status": "published", "message": "Approved and published."}

        conn.commit()
    engine.dispose()
    return {"status": "pending_review", "message": f"{approval_count}/{threshold} approvals."}


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_annotations(node_id: str) -> list[dict]:
    """Get all annotations for a node."""
    import json
    driver = get_driver()
    with driver.session() as s:
        result = s.run(
            "MATCH (n {id: $node_id}) RETURN n.annotations AS annotations",
            {"node_id": node_id},
        ).single()
    if not result or not result["annotations"]:
        return []
    annotations = result["annotations"]
    if isinstance(annotations, list):
        return [json.loads(a) if isinstance(a, str) else a for a in annotations]
    return []


def get_pending_reviews() -> list[dict]:
    """Get all contributions pending review."""
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT c.id, c.contribution_type, c.target_neo4j_node_id,
                   c.content, c.created_at, u.display_name AS contributor
            FROM contributions c
            JOIN users u ON c.user_id = u.id
            WHERE c.status = 'pending_review'
            ORDER BY c.created_at
        """)).mappings().all()
    engine.dispose()
    return [dict(r) for r in rows]
