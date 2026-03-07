"""Parts catalog lookup service.

Resolves component names from the service manual to OEM part numbers and pricing.
Data is populated by the scraper (backend/cli/scrape_parts.py) and stored in Postgres.
"""

from __future__ import annotations

import re

from sqlalchemy import create_engine, text

from backend.app.core.config import settings

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.postgres_sync_url, pool_size=3, pool_pre_ping=True)
    return _engine


def lookup_parts(
    component_name: str,
    make: str = "",
    model: str = "",
    year: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """Look up parts by component name using fuzzy text matching.

    Returns list of matching parts with OEM P/N, price, etc.
    Searches part_name, description, and subcategory fields.
    """
    engine = _get_engine()

    # Build a flexible search — component names from the manual don't always
    # match catalog names exactly (e.g. "SHIFT SOLENOID VALVE SLT" vs
    # "Automatic Transmission Control Solenoid"). Use OR matching with
    # relevance ranking so partial matches still surface.
    words = [w for w in component_name.upper().split() if len(w) > 2]
    if not words:
        return []

    # Each word is an OR condition; we rank by how many words match
    word_conditions = []
    params: dict = {"limit": limit}
    for i, word in enumerate(words[:5]):
        key = f"w{i}"
        word_conditions.append(
            f"CASE WHEN UPPER(part_name) LIKE :{key} OR UPPER(description) LIKE :{key} OR UPPER(subcategory) LIKE :{key} THEN 1 ELSE 0 END"
        )
        params[key] = f"%{word}%"

    score_expr = " + ".join(word_conditions)
    # Require at least one word to match
    any_match_conditions = []
    for i in range(len(words[:5])):
        key = f"w{i}"
        any_match_conditions.append(
            f"(UPPER(part_name) LIKE :{key} OR UPPER(description) LIKE :{key} OR UPPER(subcategory) LIKE :{key})"
        )
    where = "(" + " OR ".join(any_match_conditions) + ")"

    if make:
        where += " AND UPPER(make) = :make"
        params["make"] = re.sub(r'[^A-Z0-9]', '', make.upper())
    if model:
        where += " AND UPPER(model) = :model"
        params["model"] = re.sub(r'[^A-Z0-9]', '', model.upper())
    if year:
        where += " AND year_start <= :year AND year_end >= :year"
        params["year"] = year

    sql = f"""
        SELECT oem_part_number, part_name, description, category, subcategory,
               msrp, currency, region, price_type,
               non_reusable, quantity_per_assembly, superseded_by,
               diagram_url, callout_number, scraped_at
        FROM parts_catalog
        WHERE {where}
        ORDER BY ({score_expr}) DESC, part_name
        LIMIT :limit
    """

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    return [
        {
            "oem_part_number": row[0],
            "part_name": row[1],
            "description": row[2],
            "category": row[3],
            "subcategory": row[4],
            "msrp": row[5],
            "currency": row[6],
            "region": row[7],
            "price_type": row[8],
            "non_reusable": row[9],
            "quantity_per_assembly": row[10],
            "superseded_by": row[11],
            "diagram_url": row[12],
            "callout_number": row[13],
            "scraped_at": row[14],
        }
        for row in rows
    ]


def lookup_by_part_number(oem_part_number: str) -> dict | None:
    """Look up a specific part by its OEM part number."""
    engine = _get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT oem_part_number, part_name, description, category, subcategory, msrp, currency, non_reusable, quantity_per_assembly, superseded_by FROM parts_catalog WHERE oem_part_number = :pn"),
            {"pn": oem_part_number},
        ).fetchone()

    if not row:
        return None
    return {
        "oem_part_number": row[0],
        "part_name": row[1],
        "description": row[2],
        "category": row[3],
        "subcategory": row[4],
        "msrp": row[5],
        "currency": row[6],
        "non_reusable": row[7],
        "quantity_per_assembly": row[8],
        "superseded_by": row[9],
    }


def _get_vehicle_parts(make: str = "", model: str = "", year: int | None = None) -> list[dict]:
    """Get all parts for a vehicle from the catalog."""
    engine = _get_engine()
    where_parts = []
    params: dict = {}
    if make:
        where_parts.append("UPPER(make) = :make")
        params["make"] = re.sub(r'[^A-Z0-9]', '', make.upper())
    if model:
        where_parts.append("UPPER(model) = :model")
        params["model"] = re.sub(r'[^A-Z0-9]', '', model.upper())
    if year:
        where_parts.append("year_start <= :year AND year_end >= :year")
        params["year"] = year
    if not where_parts:
        return []
    sql = f"""
        SELECT oem_part_number, part_name, description, category, subcategory,
               msrp, currency, region, price_type,
               non_reusable, quantity_per_assembly, superseded_by,
               diagram_url, callout_number, scraped_at
        FROM parts_catalog
        WHERE {' AND '.join(where_parts)}
        ORDER BY category, part_name
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [
        {
            "oem_part_number": row[0], "part_name": row[1], "description": row[2],
            "category": row[3], "subcategory": row[4], "msrp": row[5],
            "currency": row[6], "region": row[7], "price_type": row[8],
            "non_reusable": row[9], "quantity_per_assembly": row[10],
            "superseded_by": row[11], "diagram_url": row[12],
            "callout_number": row[13], "scraped_at": row[14],
        }
        for row in rows
    ]


def get_parts_for_work_order(
    component_names: list[str],
    make: str = "",
    model: str = "",
    year: int | None = None,
) -> str:
    """Look up multiple components and format as context for the LLM.

    Returns a text block the diagnostic engine can inject into the LLM prompt
    so it can populate part numbers and prices in a work order.
    """
    lines = []
    found_any = False
    regions_seen = set()
    scraped_dates = set()
    seen_pns = set()  # Deduplicate across component lookups
    matched_categories: set[str] = set()

    for name in component_names:
        parts = lookup_parts(name, make=make, model=model, year=year, limit=5)
        for p in parts:
            if p["oem_part_number"] in seen_pns:
                continue
            seen_pns.add(p["oem_part_number"])
            found_any = True
            if p.get("category"):
                matched_categories.add(p["category"])
            _append_part_line(p, lines, regions_seen, scraped_dates)

    # Also include all parts from the SAME CATEGORY as the parts we already
    # found (one per unique name). This surfaces related items (gaskets, seals,
    # filters) without dumping the entire catalog. Scales to any vehicle/job.
    if found_any:
        if matched_categories:
            all_vehicle_parts = _get_vehicle_parts(make=make, model=model, year=year)
            seen_names: set[str] = set()
            for p in all_vehicle_parts:
                if p["oem_part_number"] in seen_pns:
                    continue
                if p.get("category") not in matched_categories:
                    continue
                name_key = p["part_name"].strip().upper()
                if name_key in seen_names:
                    continue
                seen_names.add(name_key)
                seen_pns.add(p["oem_part_number"])
                _append_part_line(p, lines, regions_seen, scraped_dates)

    if not found_any:
        return ""

    # Build header with pricing provenance
    region_notes = []
    for rgn, cur, ptype in sorted(regions_seen):
        region_notes.append(f"{rgn} {ptype} in {cur}")
    date_note = ""
    if scraped_dates:
        oldest = min(scraped_dates)
        newest = max(scraped_dates)
        if oldest == newest:
            date_note = f", as of {newest}"
        else:
            date_note = f", scraped {oldest} to {newest}"
    header = f"PARTS CATALOG DATA (OEM dealer catalog — prices are {', '.join(region_notes)}{date_note}):"

    return header + "\n" + "\n".join(lines)


def _append_part_line(p: dict, lines: list, regions_seen: set, scraped_dates: set):
    """Format a single part and append to lines list."""
    price_str = f"${p['msrp']:.2f} {p['currency']}" if p['msrp'] else "price N/A"
    superseded = f" (superseded by {p['superseded_by']})" if p['superseded_by'] else ""
    non_reusable = " [NON-REUSABLE]" if p['non_reusable'] else ""
    qty = f" (qty per assembly: {p['quantity_per_assembly']})" if p['quantity_per_assembly'] else ""
    lines.append(
        f"  • {p['part_name']} — OEM# {p['oem_part_number']} — {price_str}"
        f"{non_reusable}{qty}{superseded}"
    )
    regions_seen.add((p.get("region", "US"), p.get("currency", "USD"), p.get("price_type", "MSRP")))
    if p.get("scraped_at"):
        scraped_dates.add(p["scraped_at"].date() if hasattr(p["scraped_at"], "date") else p["scraped_at"])
