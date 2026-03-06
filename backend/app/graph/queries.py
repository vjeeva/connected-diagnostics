"""Reusable Cypher read queries."""

# Find problems for a vehicle
PROBLEMS_FOR_VEHICLE = """
MATCH (p:Problem)-[:APPLIES_TO]->(v:Vehicle {make: $make, model: $model})
WHERE v.year >= $year_start AND v.year <= $year_end
RETURN p
ORDER BY p.title
"""

# Find problems matching DTC codes
PROBLEMS_BY_DTC = """
MATCH (p:Problem)-[:APPLIES_TO]->(v:Vehicle {make: $make, model: $model})
WHERE ANY(code IN p.dtc_codes WHERE code IN $dtc_codes)
  AND v.year >= $year_start AND v.year <= $year_end
RETURN p
"""

# Get children of a diagnostic node (next steps in the tree)
NODE_CHILDREN = """
MATCH (n {id: $node_id})-[r:LEADS_TO]->(child)
RETURN child, r.condition AS condition, r.confidence AS confidence,
       labels(child)[0] AS node_type
"""

# Get solution with its steps and parts
SOLUTION_DETAILS = """
MATCH (s:Solution {id: $solution_id})
OPTIONAL MATCH (s)-[ns:NEXT_STEP]->(step:Step)
OPTIONAL MATCH (step)-[:REQUIRES_PART]->(part:Part)
OPTIONAL MATCH (step)-[:REQUIRES_TOOL]->(tool:Tool)
OPTIONAL MATCH (s)-[:REQUIRES_PART]->(sPart:Part)
OPTIONAL MATCH (s)-[:REQUIRES_TOOL]->(sTool:Tool)
RETURN s, step, ns.step_order AS step_order, part, tool, sPart, sTool
ORDER BY ns.step_order
"""

# Get a node by ID with its labels
NODE_BY_ID = """
MATCH (n {id: $node_id})
RETURN n, labels(n)[0] AS node_type
"""

# Get the full diagnostic path from a problem
DIAGNOSTIC_TREE = """
MATCH path = (p:Problem {id: $problem_id})-[:LEADS_TO*1..10]->(leaf)
WHERE NOT (leaf)-[:LEADS_TO]->()
RETURN path
"""
