"""Trace P1603 lean condition path: engine slowly stalls at idle."""
from neo4j import GraphDatabase
from backend.app.core.config import settings

driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def get_children(session, eid):
    return session.run("""
        MATCH (a)-[r:LEADS_TO|NEXT_STEP]->(b)
        WHERE elementId(a) = $eid
        RETURN elementId(b) AS eid, labels(b)[0] AS label, b.title AS title,
               type(r) AS rtype, r.condition AS condition,
               b.instruction AS instruction, b.expected_result AS expected,
               b.tool_required AS tool, b.procedure AS procedure
        ORDER BY r.condition, b.title
    """, {"eid": eid}).data()


def pick(nodes, **kwargs):
    """Find a node matching criteria."""
    for n in nodes:
        match = True
        for key, val in kwargs.items():
            field = n.get(key) or n.get("title", "")
            if val not in field:
                match = False
                break
        if match:
            return n
    return None


def print_step(num, node, condition=None):
    print(f"\n{'=' * 70}")
    print(f"STEP {num}: [{node['label']}] {node['title']}")
    if condition:
        print(f"  BECAUSE: {condition}")
    print("=" * 70)
    if node.get("instruction"):
        print(f"\n  WHAT TO DO:")
        # Format instruction nicely
        instr = node["instruction"]
        for line in instr.split(". "):
            line = line.strip()
            if line:
                print(f"    {line}.")
    if node.get("expected"):
        print(f"\n  WHAT TO EXPECT:")
        print(f"    {node['expected']}")
    if node.get("tool"):
        print(f"\n  TOOLS NEEDED:")
        print(f"    {node['tool']}")
    if node.get("procedure"):
        print(f"\n  REPAIR PROCEDURE:")
        for line in node["procedure"][:1500].split("\n"):
            if line.strip():
                print(f"    {line}")


with driver.session() as s:
    step = 0

    # STEP 0: Problem
    root = s.run(
        'MATCH (p:Problem) WHERE "P1603" IN p.dtc_codes '
        'RETURN elementId(p) AS eid, p.title AS title, p.description AS desc, p.dtc_codes AS dtcs'
    ).single()

    print("=" * 70)
    print(f"DTC P1603 DIAGNOSTIC WALKTHROUGH")
    print(f"Scenario: Engine slowly stalls at idle — lean condition")
    print("=" * 70)
    print(f"\n[Problem] {root['title']}")
    print(f"DTCs: {root['dtcs']}")
    print(f"\n{root['desc']}")

    eid = root["eid"]

    # STEP 1: Check for other DTCs
    step += 1
    children = get_children(s, eid)
    node = pick(children, title="CHECK FOR ANY OTHER DTCS")
    print_step(step, node)

    # STEP 2: Result — Only P1603
    step += 1
    children = get_children(s, node["eid"])
    node = pick(children, condition="Only DTC P1603")
    print_step(step, node, "Only DTC P1603 and/or P1605 is output")

    # STEP 3: Check Freeze Frame Data
    step += 1
    children = get_children(s, node["eid"])
    node = pick(children, title="CHECK FREEZE FRAME DATA")
    print_step(step, node)

    # STEP 4: Normal immobiliser → READ FREEZE FRAME DATA
    step += 1
    children = get_children(s, node["eid"])
    node = pick(children, condition="Normal")
    print_step(step, node, "Immobiliser Fuel Cut is OFF (Normal)")

    # STEP 5: Read freeze frame → identify stall pattern
    children = get_children(s, node["eid"])
    step += 1
    print_step(step, node)

    # Show the diagnostic fork
    print("\n  " + "-" * 66)
    print("  DIAGNOSTIC FORK — How does the engine stall?")
    print("  " + "-" * 66)
    results = [c for c in children if c["label"] == "Result"]
    for r in results:
        print(f"    → {r['title']}")
    print()
    print("  >>> Our scenario: Engine speed SLOWLY decreases, LEAN condition")

    # STEP 6: Lean result
    step += 1
    node = pick(children, title="slowly decreases and engine stalls - Air suction")
    print_step(step, node, "Engine speed slowly decreases → Air suction / Lean / Fuel supply problem")

    # STEP 7: CHECK INTAKE SYSTEM
    step += 1
    children = get_children(s, node["eid"])
    node = pick(children, title="CHECK INTAKE SYSTEM")
    print_step(step, node)

    # Follow OK path (no air leak) → deeper diagnosis
    step += 1
    children = get_children(s, node["eid"])

    # Show fork
    print(f"\n  " + "-" * 66)
    print("  INTAKE SYSTEM CHECK RESULT:")
    for c in children:
        cond = c.get("condition") or ""
        print(f"    [{cond}] → [{c['label']}] {c['title']}")

    # Follow OK to EGR test
    ok_node = pick(children, condition="OK", title="CHECK THROTTLE")
    if not ok_node:
        ok_node = pick(children, label="Test", title="PERFORM ACTIVE TEST")
    if not ok_node:
        # Try the next step path
        ok_node = pick(children, label="Test")

    # Let's follow the EGR path — common lean cause
    egr_node = pick(children, title="EGR STEP POSITION")
    if egr_node:
        node = egr_node
        print(f"\n  >>> Intake OK, checking EGR system (common lean cause)")
        print_step(step, node)

        # EGR test result
        step += 1
        children = get_children(s, node["eid"])
        print(f"\n  " + "-" * 66)
        print("  EGR TEST RESULT:")
        for c in children:
            cond = c.get("condition") or ""
            print(f"    [{cond}] → [{c['label']}] {c['title']}")

        # NG path — EGR is stuck/bad
        ng_node = pick(children, condition="NG")
        if ng_node:
            node = ng_node
            print(f"\n  >>> EGR not functioning correctly")
            print_step(step, node, "NG — EGR valve not responding properly")

            # Follow EGR inspection path
            children = get_children(s, node["eid"])
            for c in children:
                step += 1
                print_step(step, c, c.get("condition"))
                next_children = get_children(s, c["eid"])
                for nc in next_children:
                    step += 1
                    print_step(step, nc, nc.get("condition"))
                    if nc["label"] == "Solution":
                        break
                    nc_children = get_children(s, nc["eid"])
                    for nnc in nc_children:
                        step += 1
                        print_step(step, nnc, nnc.get("condition"))
                        if nnc["label"] == "Solution":
                            break
                        nnc_children = get_children(s, nnc["eid"])
                        for nnnc in nnc_children:
                            step += 1
                            print_step(step, nnnc, nnnc.get("condition"))
                            if nnnc["label"] == "Solution":
                                break
                    break  # just follow one path
                break  # just follow one path

    print("\n" + "=" * 70)
    print("END OF DIAGNOSTIC PATH")
    print("=" * 70)

driver.close()
