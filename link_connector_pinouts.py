"""Link connector pinout data to Test nodes that reference terminal measurements.

Finds Test nodes with generic terminal references (e.g. "terminals 1-2")
and searches the chunk corpus for the corresponding connector pin mapping,
voltage/resistance tables, and connector front-view references.
"""
import re

from neo4j import GraphDatabase
from backend.app.core.config import settings
from backend.app.services.search_service import search_chunks

driver = GraphDatabase.driver(
    settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
)


def extract_component(title: str) -> str | None:
    """Extract the component name from a Test node title.

    'INSPECT EGR VALVE ASSEMBLY' -> 'EGR VALVE ASSEMBLY'
    'CHECK HARNESS AND CONNECTOR (ECM - MASS AIR FLOW METER)' -> 'MASS AIR FLOW METER'
    """
    # Pattern: CHECK HARNESS AND CONNECTOR (X - Y) -> Y is the component
    m = re.search(r'\(.*?-\s*(.+?)\)', title)
    if m:
        return m.group(1).strip()

    # Pattern: INSPECT X / CHECK X
    for prefix in ["INSPECT ", "CHECK "]:
        if title.startswith(prefix):
            return title[len(prefix):].strip()

    return title


def extract_pinout_from_chunk(chunk_text: str, component: str) -> str | None:
    """Extract connector pin mapping, voltage/resistance tables from chunk text."""
    lines = chunk_text.split("\n")
    component_lower = component.lower()

    # Collect sections with: connector IDs, tester connections, standard voltage/resistance,
    # front view references
    relevant_sections = []
    capturing = False
    current_section = []

    for i, line in enumerate(lines):
        ll = line.lower().strip()

        # Start capturing at measurement-related headers
        is_measurement_start = any(kw in ll for kw in [
            "standard voltage:", "standard resistance:", "tester connection",
            "front view of wire harness", "front view of connector",
        ])

        # Also start at lines with connector pin IDs near component name
        has_pin_id = bool(re.search(r'[A-Z]\d+-\d+', line))

        if is_measurement_start:
            if current_section:
                relevant_sections.append("\n".join(current_section))
            current_section = [line]
            capturing = True
        elif capturing and has_pin_id:
            current_section.append(line)
        elif capturing and any(kw in ll for kw in [
            "specified condition", "condition", "always", "engine switch",
            "below 1", "10 k", "11 to 14", "body ground", "illustration",
        ]):
            current_section.append(line)
        elif capturing and ll in ("ok", "ng", ""):
            if current_section and len(current_section) > 1:
                relevant_sections.append("\n".join(current_section))
            current_section = []
            capturing = False
        elif capturing:
            current_section.append(line)

    if current_section and len(current_section) > 1:
        relevant_sections.append("\n".join(current_section))

    if not relevant_sections:
        return None

    return "\n\n".join(relevant_sections)


# 1. Find Test nodes that need connector pinout data
with driver.session() as s:
    tests = s.run("""
        MATCH (t:Test)
        WHERE t.instruction IS NOT NULL
        AND t.connector_info IS NULL
        AND (t.instruction CONTAINS 'terminals 1-2' OR t.instruction CONTAINS 'terminal 1'
             OR t.instruction CONTAINS 'terminals 1' OR t.instruction CONTAINS 'tester to terminals'
             OR (t.instruction CONTAINS 'Measure the resistance'
                 AND NOT t.instruction =~ '.*[A-Z][0-9]+-[0-9]+.*'))
        RETURN elementId(t) AS eid, t.title AS title, t.instruction AS instruction,
               t.expected_result AS expected
    """).data()

print(f"Test nodes needing connector pinout: {len(tests)}")

linked = 0
skipped = 0

for test in tests:
    title = test["title"]
    component = extract_component(title)

    if not component or len(component) < 3:
        skipped += 1
        continue

    # Search for connector/pinout info for this component
    search_query = f"{component} connector terminal pin voltage resistance front view"
    try:
        results = search_chunks(search_query, limit=5)
    except Exception as e:
        print(f"  Search error for '{component}': {e}")
        skipped += 1
        continue

    # Find chunks that mention the component and have connector pin data
    component_lower = component.lower()
    best_pinout = None
    best_chunk = None

    for r in results:
        text = r["chunk_text"]
        if component_lower not in text.lower():
            continue

        # Check for connector pin patterns
        has_pins = bool(re.search(r'[A-Z]\d+-\d+', text))
        has_table = any(kw in text.lower() for kw in [
            "standard voltage:", "standard resistance:", "tester connection",
            "front view",
        ])

        if has_pins and has_table:
            pinout = extract_pinout_from_chunk(text, component)
            if pinout and len(pinout) > 20:
                best_pinout = pinout
                best_chunk = r
                break

    if not best_pinout:
        skipped += 1
        continue

    # Store on the Test node
    with driver.session() as s:
        s.run("""
            MATCH (t) WHERE elementId(t) = $eid
            SET t.connector_info = $pinout,
                t.connector_info_page = $page,
                t.connector_info_source = 'auto-linked from chunk'
        """, {
            "eid": test["eid"],
            "pinout": best_pinout[:3000],
            "page": best_chunk["page_number"],
        })

    linked += 1
    print(f"  [{linked}] {title} -> p.{best_chunk['page_number']} ({len(best_pinout)} chars)")

print(f"\nLinked: {linked}, Skipped: {skipped}, Total: {len(tests)}")

# Show samples
print("\n=== SAMPLE LINKED TEST ===")
with driver.session() as s:
    samples = s.run("""
        MATCH (t:Test) WHERE t.connector_info IS NOT NULL
        RETURN t.title AS title, t.connector_info AS info, t.connector_info_page AS page
        LIMIT 3
    """).data()
    for sample in samples:
        print(f"\n[Test] {sample['title']} (from p.{sample['page']})")
        print(f"  {sample['info'][:500]}")

driver.close()
