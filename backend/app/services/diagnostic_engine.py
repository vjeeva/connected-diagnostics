"""Core diagnostic engine — drives the conversation through the knowledge graph."""

from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from backend.app.core.config import settings
from backend.app.db.neo4j_client import run_query
from backend.app.graph import queries
from backend.app.services.llm.client import chat, chat_stream, interpret
from backend.app.services.llm.prompts import (
    DIAGNOSTIC_SYSTEM,
    INTERPRET_SYSTEM,
    VEHICLE_EXTRACT_SYSTEM,
)
from backend.app.services.parts_catalog import get_parts_for_work_order
from backend.app.services.search_service import search_chunks, search_chunks_keyword
from backend.app.services.shop_rules import get_rules_for_prompt


def _extract_relevant_window(chunk_text: str, query: str, window: int = 6000) -> str:
    """Extract the most relevant portion(s) of a chunk.
    For large chunks, returns two windows: the best match for the query
    PLUS installation/torque specs if they exist elsewhere in the chunk."""
    if len(chunk_text) <= window:
        return chunk_text

    text_upper = chunk_text.upper()
    # Extract meaningful words from the query (skip short/common words)
    words = [w for w in re.findall(r'\w+', query) if len(w) > 3]
    # Also boost procedure-related headings so we capture access/removal steps
    structural = ["REMOVAL", "PROCEDURE", "DRAIN", "DISASSEMBLY", "INSTALL"]

    half = window // 2

    # Find the position with the highest density of query term matches
    best_pos = 0
    best_score = -1
    step = 200
    for pos in range(0, len(chunk_text) - half + 1, step):
        segment = text_upper[pos:pos + half]
        score = sum(segment.count(w.upper()) for w in words)
        score += sum(0.5 * segment.count(s) for s in structural)
        if score > best_score:
            best_score = score
            best_pos = pos

    if best_score <= 0:
        return chunk_text[:window]

    primary = chunk_text[best_pos:best_pos + half]

    # Find a second window for installation/torque content if not already covered
    torque_words = ["TORQUE", "INSTALL", "N·M", "FT·LBF", "IN·LBF"]
    second_pos = -1
    second_score = -1
    for pos in range(0, len(chunk_text) - half + 1, step):
        # Skip if overlapping with primary window
        if abs(pos - best_pos) < half:
            continue
        segment = text_upper[pos:pos + half]
        score = sum(segment.count(w) for w in torque_words)
        if score > second_score:
            second_score = score
            second_pos = pos

    if second_score > 2 and second_pos >= 0:
        secondary = chunk_text[second_pos:second_pos + half]
        return primary + "\n\n---\n\n" + secondary

    # No useful second window — use full budget on primary
    return chunk_text[best_pos:best_pos + window]


_AFFIRMATIVES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please",
    "go ahead", "do it", "yes please", "sure thing", "absolutely",
    "definitely", "right", "correct", "go for it", "sounds good",
})


def _chat_or_stream(on_token, model: str | None = None, **kwargs) -> str:
    """Call chat() or chat_stream() depending on whether a streaming callback is set."""
    if model:
        kwargs["model"] = model
    if on_token:
        parts = []
        for chunk in chat_stream(**kwargs):
            on_token(chunk)
            parts.append(chunk)
        return "".join(parts)
    return chat(**kwargs)


_ACTION_RE = re.compile(
    r'\b(remov|replac|disassembl|disconnect|drain|unbolt|detach|'
    r'install|reassembl|measur|inspect|access)\w*\b', re.IGNORECASE
)

_WORK_ORDER_RE = re.compile(
    r'\b(work\s*order|estimate|quote|invoice|parts\s*list|cost|price|how\s*much)\b',
    re.IGNORECASE,
)

_WO_DIRECTIVE = (
    "\n\n[SYSTEM DIRECTIVE: Generate the COMPLETE work order NOW using the WORK ORDER "
    "FORMAT from your instructions. Do NOT ask for confirmation or clarification. "
    "Include ALL sections: TASK BREAKDOWN, PARTS REQUIRED, and SUMMARY. "
    "CRITICAL REQUIREMENTS: "
    "1) Every subtask bullet MUST end with (XX min) time estimate. "
    "2) MATH CHECK: Add up all subtask minutes in each task group. Convert to hours. "
    "The task group header MUST show that exact sum. Example: if subtasks are 15+15+20+10=60 min, "
    "header must say 1.0 hrs. Do the arithmetic for EVERY group. "
    "3) Only list parts with real OEM part numbers from the PARTS CATALOG DATA — NEVER use TBD. "
    "4) ATF drain goes in the access/disassembly task group ONLY. The ATF Refill task group "
    "must contain ONLY refill/fill steps — never mention draining in that group. "
    "5) Include explicit subtasks for: raise vehicle on lift, disconnect harness/connectors, "
    "remove oil pan, drain ATF. "
    "6) Include ACTUAL torque values with units (e.g. '7.0 N·m', '10 N·m', '21 N·m') "
    "from the service manual context for every reassembly step. Do NOT say 'per manual specs' — "
    "write the actual number and unit. "
    "Start output with 'WORK ORDER —'.]"
)

# Common automotive component terms to extract from conversation context
_COMPONENT_RE = re.compile(
    r'\b(?:shift\s+)?solenoid\s+(?:valve\s+)?[A-Z0-9]+\b'
    r'|\b(?:valve\s+body|oil\s+pan|oil\s+strainer|gasket|o-ring|'
    r'transmission\s+fluid|atf|sensor|filter|seal|bearing|clutch|'
    r'torque\s+converter|cooler|hose|connector|wire\s+harness)\b',
    re.IGNORECASE,
)


def _extract_component_names(text: str) -> list[str]:
    """Extract component names from conversation context for parts catalog lookup."""
    matches = _COMPONENT_RE.findall(text)
    # Deduplicate while preserving order, normalize to title case
    seen = set()
    names = []
    for m in matches:
        key = m.strip().upper()
        if key not in seen and len(key) > 2:
            seen.add(key)
            names.append(m.strip())
    return names


def _search_procedure_context(node: dict, problem_title: str = "") -> str:
    """When a node involves physical work (remove, replace, measure),
    proactively search for the access/removal procedure so the LLM
    can include all prerequisite steps without the user having to ask."""
    title = node.get("title", "")
    instruction = node.get("instruction", "")
    node_text = f"{title} {instruction}"

    if not _ACTION_RE.search(node_text):
        return ""

    # Semantic searches for removal/access procedures
    # Include "valve body" as a common parent assembly term — solenoids,
    # sensors, and other internals are accessed through the valve body
    search_queries = [
        f"{title} removal procedure disassembly valve body",
        f"{title} {problem_title} removal access drain",
    ]

    chunks = []
    existing_ids: set[str] = set()
    with ThreadPoolExecutor(max_workers=len(search_queries)) as pool:
        futures = [pool.submit(search_chunks, q, None, 3) for q in search_queries]
        for f in as_completed(futures):
            for c in f.result():
                if c["id"] not in existing_ids:
                    chunks.append(c)
                    existing_ids.add(c["id"])

    if not chunks:
        return ""

    window_q = f"{title} removal procedure drain disassembly torque"
    return "\n\n---\n\n".join(
        _extract_relevant_window(c["chunk_text"], window_q)
        for c in chunks[:5]
    )


def _effective_search_query(user_input: str, messages: list[dict]) -> str:
    """When user input is a short affirmative or contains vague references
    (pronouns like 'that', 'this', 'it'), enrich the search with the
    assistant's last message — that's what the user is referring to."""
    normalized = user_input.strip().lower().rstrip(".,!?")
    use_assistant = (
        normalized in _AFFIRMATIVES
        or len(normalized) <= 20
        # Detect vague pronoun references — user says "that solenoid" or
        # "how do I remove it" without naming the specific component
        or re.search(r'\b(that|this|those|it)\b', normalized)
        # Detect how-to / action questions — user is asking about something
        # the assistant just mentioned (e.g. "how do i remove S1")
        or re.search(r'\bhow (do|can|to|would)\b', normalized)
    )
    if not use_assistant:
        return user_input

    # Combine user input with assistant's last message for richer context
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            assistant_tail = msg["content"][-500:]
            # For short/affirmative: use mostly assistant context
            if normalized in _AFFIRMATIVES or len(normalized) <= 20:
                return assistant_tail
            # For vague references: combine both so user's intent is preserved
            return f"{user_input} {assistant_tail}"
    return user_input


@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    vehicle_neo4j_id: str = ""
    vehicle_info: dict = field(default_factory=dict)
    current_node_id: str = ""
    current_node_type: str = ""
    problem_neo4j_id: str = ""
    step_order: int = 0
    phase: str = "diagnosis"  # diagnosis | estimate | completed
    messages: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)


def extract_vehicle_and_problem(user_input: str) -> dict:
    """Use LLM to extract vehicle info and problem description from user input."""
    raw = interpret(system=VEHICLE_EXTRACT_SYSTEM, messages=[{"role": "user", "content": user_input}])
    try:
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"problem_description": user_input, "symptoms": [], "dtc_codes": []}


def find_matching_problems(problem_desc: str, vehicle_id: str | None = None) -> list[dict]:
    """Search for matching problems using semantic search + graph lookup."""
    # Semantic search over chunks
    chunks = []
    if problem_desc:
        try:
            chunks = search_chunks(problem_desc, vehicle_neo4j_id=vehicle_id, limit=5)
        except Exception:
            pass

    # Collect unique neo4j node IDs from matching chunks
    node_ids = set()
    for chunk in chunks:
        if chunk.get("neo4j_node_id"):
            node_ids.add(chunk["neo4j_node_id"])

    # Also search for Problem nodes directly in Neo4j by description
    query_str = (problem_desc or "")[:100]
    problems = run_query(
        "MATCH (p:Problem) WHERE toLower(p.title) CONTAINS toLower($query) "
        "OR toLower(p.description) CONTAINS toLower($query) "
        "RETURN p LIMIT 10",
        {"query": query_str},
    ) if query_str else []

    # Also search by individual DTC codes (P2714 won't match "P2714 - transmission...")
    dtc_codes = re.findall(r'[PBCUp]\d{4}', problem_desc or "")
    for code in dtc_codes:
        code_problems = run_query(
            "MATCH (p:Problem) WHERE p.title CONTAINS $code RETURN p LIMIT 5",
            {"code": code},
        )
        problems.extend(code_problems)

    # Combine results
    all_problems = []
    seen = set()
    for p in problems:
        node = p.get("p", {})
        nid = node.get("id", "")
        if nid and nid not in seen:
            seen.add(nid)
            all_problems.append(node)

    return all_problems


def get_node_children(node_id: str) -> list[dict]:
    """Get the child nodes connected via LEADS_TO from the current node."""
    results = run_query(queries.NODE_CHILDREN, {"node_id": node_id})
    children = []
    for r in results:
        child = r.get("child", {})
        child["_node_type"] = r.get("node_type", "")
        child["_condition"] = r.get("condition", "")
        child["_confidence"] = r.get("confidence", 0)
        children.append(child)
    return children


def get_node(node_id: str) -> dict | None:
    """Fetch a single node by ID."""
    results = run_query(queries.NODE_BY_ID, {"node_id": node_id})
    if results:
        node = results[0].get("n", {})
        node["_node_type"] = results[0].get("node_type", "")
        return node
    return None


def start_session(user_input: str, on_token=None, on_status=None) -> tuple[SessionState, str]:
    """Start a new diagnostic session from the user's initial problem description.

    Returns (session_state, assistant_response).
    If on_token callback is provided, streams response tokens through it.
    If on_status callback is provided, sends progress updates (e.g. spinner text).
    """
    _status = on_status or (lambda msg: None)
    state = SessionState()

    # Extract vehicle + problem info
    _status("Extracting vehicle info...")
    extracted = extract_vehicle_and_problem(user_input)
    state.vehicle_info = extracted

    # Find matching problems
    _status("Searching knowledge graph...")
    problem_desc = extracted.get("problem_description") or user_input
    dtc_codes = extracted.get("dtc_codes") or []
    problems = find_matching_problems(problem_desc)

    if not problems:
        # No graph matches — use search context for LLM response
        _status("Searching chunks database...")
        chunks = search_chunks(problem_desc, limit=5)

        # Work orders need extra searches for procedures, torque, parts
        if _WORK_ORDER_RE.search(user_input):
            existing_ids = {c["id"] for c in chunks}
            wo_queries = [
                f"{problem_desc} removal installation procedure steps",
                f"{problem_desc} torque specification fluid capacity ATF",
                f"{problem_desc} parts required OEM part number",
            ]
            with ThreadPoolExecutor(max_workers=len(wo_queries)) as pool:
                futures = [pool.submit(search_chunks, q, None, 3) for q in wo_queries]
                for f in as_completed(futures):
                    for rc in f.result():
                        if rc["id"] not in existing_ids:
                            chunks.append(rc)
                            existing_ids.add(rc["id"])

        context = "\n\n---\n\n".join(_extract_relevant_window(c["chunk_text"], problem_desc) for c in chunks) if chunks else "No relevant information found."

        context_msg = f"[SERVICE MANUAL REFERENCE — not from user]:\n{context}\n\n"

        # Parts lookup for work orders
        if _WORK_ORDER_RE.search(user_input):
            _status("Looking up parts catalog...")
            component_names = _extract_component_names(f"{problem_desc} {user_input}")
            vehicle = state.vehicle_info
            parts_context = get_parts_for_work_order(
                component_names,
                make=vehicle.get("make", ""),
                model=vehicle.get("model", ""),
                year=vehicle.get("year"),
            )
            if parts_context:
                context_msg += f"{parts_context}\n\n"

        tech_rules = get_rules_for_prompt()
        if tech_rules:
            context_msg += f"{tech_rules}\n\n"
        response = _chat_or_stream(
            on_token,
            model=settings.chat_model,  # strong: no graph match, open-ended reasoning
            system=DIAGNOSTIC_SYSTEM,
            messages=[
                {"role": "user", "content": f"{context_msg}User's problem: {user_input}{_WO_DIRECTIVE if _WORK_ORDER_RE.search(user_input) else ''}"}
            ],
        )
        state.messages.append({"role": "user", "content": user_input})
        state.messages.append({"role": "assistant", "content": response})
        return state, response

    # Pick the best matching problem (first one for now)
    problem = problems[0]
    state.problem_neo4j_id = problem.get("id", "")
    state.current_node_id = state.problem_neo4j_id
    state.current_node_type = "Problem"

    # Get children of the problem node
    _status("Traversing knowledge graph...")
    children = get_node_children(state.current_node_id)

    # Format first diagnostic question using LLM
    children_desc = "\n".join(
        f"- [{c.get('_node_type', '')}] {c.get('title', '')} | "
        f"Condition: {c.get('_condition', 'N/A')} | "
        f"Question: {c.get('question_text', c.get('instruction', ''))}"
        for c in children
    )

    # If no graph children, search chunks for procedure details + repair procedures
    chunk_context = ""
    if not children:
        _status("Searching chunks database...")
        prob_title = problem.get('title', '')
        prob_desc = problem.get('description', '')
        dtc_codes = re.findall(r'[PBCUp]\d{4}', prob_title)
        chunks = []

        # 1. Keyword search for DTC codes
        if dtc_codes:
            for code in dtc_codes:
                chunks.extend(search_chunks_keyword(code, limit=3))
            seen = set()
            chunks = [c for c in chunks if not (c['id'] in seen or seen.add(c['id']))]

        # 2. Semantic search for diagnostic procedure
        if len(chunks) < 3:
            search_q = f"{prob_desc} {prob_title} diagnostic procedure"
            chunks.extend(search_chunks(search_q, limit=5 - len(chunks)))

        # 3. Also search for repair/removal procedures so the LLM can give
        #    actionable steps immediately instead of offering to "look them up"
        repair_q = f"{prob_desc} {prob_title} removal replacement procedure steps"
        repair_chunks = search_chunks(repair_q, limit=3)
        existing_ids = {c["id"] for c in chunks}
        for rc in repair_chunks:
            if rc["id"] not in existing_ids:
                chunks.append(rc)

        # 4. Work order — also search for parts, torque specs, fluid capacity
        if _WORK_ORDER_RE.search(user_input):
            wo_queries = [
                f"{prob_desc} {prob_title} parts required OEM part number",
                f"{prob_desc} {prob_title} torque specification fluid capacity ATF",
                f"{prob_desc} {prob_title} removal installation procedure steps",
            ]
            existing_ids = {c["id"] for c in chunks}
            with ThreadPoolExecutor(max_workers=len(wo_queries)) as pool:
                futures = [pool.submit(search_chunks, q, None, 3) for q in wo_queries]
                for f in as_completed(futures):
                    for rc in f.result():
                        if rc["id"] not in existing_ids:
                            chunks.append(rc)
                            existing_ids.add(rc["id"])

        _q = f"{prob_desc} {prob_title}"
        chunk_context = "\n\n---\n\n".join(_extract_relevant_window(c["chunk_text"], _q) for c in chunks[:10]) if chunks else ""

    context = (
        f"Problem identified: {problem.get('title', '')}\n"
        f"Description: {problem.get('description', '')}\n\n"
        f"Available diagnostic paths:\n{children_desc}"
    )
    if chunk_context:
        context += f"\n\nDetailed procedure from service manual:\n{chunk_context}"

    # Work orders need chunk context even when graph children exist —
    # graph nodes only have titles, but work orders need procedures, torque specs, parts
    if _WORK_ORDER_RE.search(user_input):
        _status("Searching for procedures and specifications...")
        prob_title = problem.get('title', '')
        prob_desc = problem.get('description', '')
        wo_chunks: list[dict] = []
        wo_existing_ids: set[str] = set()
        wo_queries = [
            f"{prob_desc} {prob_title} removal installation procedure steps",
            f"{prob_desc} {prob_title} torque specification fluid capacity ATF",
            f"{prob_desc} {prob_title} parts required OEM part number",
        ]
        with ThreadPoolExecutor(max_workers=len(wo_queries)) as pool:
            futures = [pool.submit(search_chunks, q, None, 3) for q in wo_queries]
            for f in as_completed(futures):
                for rc in f.result():
                    if rc["id"] not in wo_existing_ids:
                        wo_chunks.append(rc)
                        wo_existing_ids.add(rc["id"])
        if wo_chunks:
            _q = f"{prob_desc} {prob_title} removal torque parts"
            wo_context = "\n\n---\n\n".join(
                _extract_relevant_window(c["chunk_text"], _q)
                for c in wo_chunks[:8]
            )
            context += f"\n\nDetailed procedure from service manual:\n{wo_context}"

    # Inject technician corrections
    tech_rules = get_rules_for_prompt()
    if tech_rules:
        context += f"\n\n{tech_rules}"

    # Use strong model for work orders, light for graph traversal
    is_wo = _WORK_ORDER_RE.search(user_input)
    use_model = settings.chat_model if is_wo else settings.light_model

    # Parts lookup for work orders
    if is_wo:
        _status("Looking up parts catalog...")
        prob_title = problem.get("title", "")
        prob_desc = problem.get("description", "")
        component_names = _extract_component_names(f"{prob_title} {prob_desc} {user_input}")
        vehicle = state.vehicle_info
        parts_data = get_parts_for_work_order(
            component_names,
            make=vehicle.get("make", ""),
            model=vehicle.get("model", ""),
            year=vehicle.get("year"),
        )
        if parts_data:
            context += f"\n\n{parts_data}"

    response = _chat_or_stream(
        on_token,
        model=use_model,
        system=DIAGNOSTIC_SYSTEM,
        messages=[
            {"role": "user", "content": f"[SERVICE MANUAL REFERENCE — not from user]:\n{context}\n\nUser said: {user_input}{_WO_DIRECTIVE if is_wo else ''}"},
        ],
    )

    state.step_order = 1
    state.steps.append({
        "step_order": state.step_order,
        "neo4j_node_id": state.current_node_id,
        "node_type": "Problem",
    })
    state.messages.append({"role": "user", "content": user_input})
    state.messages.append({"role": "assistant", "content": response})

    return state, response


def continue_session(state: SessionState, user_input: str, on_token=None, on_status=None) -> tuple[SessionState, str]:
    """Process the next turn in a diagnostic session.

    Returns (updated_state, assistant_response).
    If on_token callback is provided, streams response tokens through it.
    If on_status callback is provided, sends progress updates (e.g. spinner text).
    """
    _status = on_status or (lambda msg: None)
    if state.phase != "diagnosis":
        return state, "This session has already reached a conclusion."

    # Get children of current node
    _status("Traversing knowledge graph...")
    children = get_node_children(state.current_node_id)

    if not children:
        # We're at a leaf — check if it's a Solution
        current = get_node(state.current_node_id)
        if current and current.get("_node_type") == "Solution":
            state.phase = "estimate"
            return state, _format_solution_reached(current)

        # Dead end — search for repair procedures AND diagnostic context
        title = current.get('title', '') if current else ""
        description = current.get('description', '') if current else ""

        # Get the assistant's last message for context-aware search
        assistant_context = ""
        for msg in reversed(state.messages):
            if msg["role"] == "assistant":
                assistant_context = msg["content"][-500:]
                break

        # 1. Direct search for what the user is asking about RIGHT NOW
        #    Run all semantic queries in parallel to cut embedding latency
        _status("Searching chunks database...")
        chunks = []
        existing_ids = set()
        search_context = _effective_search_query(user_input, state.messages)
        search_qs = [q for q in [user_input, search_context, f"{assistant_context} {user_input}"] if q.strip()]
        with ThreadPoolExecutor(max_workers=len(search_qs)) as pool:
            futures = [pool.submit(search_chunks, q, None, 3) for q in search_qs]
            for f in as_completed(futures):
                for rc in f.result():
                    if rc["id"] not in existing_ids:
                        chunks.append(rc)
                        existing_ids.add(rc["id"])

        # 2. Keyword search for DTC codes
        dtc_codes = re.findall(r'[PBCUp]\d{4}', title)
        if dtc_codes:
            for code in dtc_codes:
                for kc in search_chunks_keyword(code, limit=2):
                    if kc["id"] not in existing_ids:
                        chunks.append(kc)
                        existing_ids.add(kc["id"])

        # 3. Semantic search combining problem context with conversation
        if len(chunks) < 5:
            search_query = f"{description} {title} {search_context}".strip()
            for sc in search_chunks(search_query, limit=5 - len(chunks)):
                if sc["id"] not in existing_ids:
                    chunks.append(sc)
                    existing_ids.add(sc["id"])

        # 4. Proactive procedure search — when user or assistant mentions
        #    physical work, search specifically for removal/access procedures
        _status("Searching for procedures...")
        combined_context = f"{user_input} {assistant_context}"
        if _ACTION_RE.search(combined_context):
            # Build focused removal query using problem description (has
            # subsystem context like "Transmission") + end of assistant context
            # (has component names like "solenoid S1") + assembly keywords
            removal_q = f"{description} {assistant_context[-150:]} removal procedure disassembly valve body"
            for rc in search_chunks(removal_q, limit=5):
                if rc["id"] not in existing_ids:
                    chunks.append(rc)
                    existing_ids.add(rc["id"])

        # 5. Work order / estimate request — search aggressively for parts,
        #    removal procedures, torque specs, and fluid capacities
        parts_context = ""
        if _WORK_ORDER_RE.search(f"{user_input} {assistant_context}"):
            _status("Gathering parts and procedures for work order...")
            wo_queries = [
                f"{description} {title} parts required OEM part number",
                f"{description} {title} torque specification fluid capacity",
                f"{description} {title} removal installation procedure steps",
            ]
            with ThreadPoolExecutor(max_workers=len(wo_queries)) as pool:
                futures = [pool.submit(search_chunks, q, None, 3) for q in wo_queries]
                for f in as_completed(futures):
                    for rc in f.result():
                        if rc["id"] not in existing_ids:
                            chunks.append(rc)
                            existing_ids.add(rc["id"])

            # Look up OEM part numbers and prices from the local parts catalog
            _status("Looking up parts catalog...")
            component_names = _extract_component_names(f"{title} {description} {assistant_context}")
            vehicle = state.vehicle_info
            parts_context = get_parts_for_work_order(
                component_names,
                make=vehicle.get("make", ""),
                model=vehicle.get("model", ""),
                year=vehicle.get("year"),
            )

        _q = f"{user_input} {assistant_context} {title}"
        chunk_context = "\n\n---\n\n".join(_extract_relevant_window(c["chunk_text"], _q) for c in chunks[:10]) if chunks else ""

        context_msg = f"[SERVICE MANUAL REFERENCE — not from user]:\n{chunk_context}\n\n" if chunk_context else ""
        if parts_context:
            context_msg += f"{parts_context}\n\n"
        # Inject technician corrections
        tech_rules = get_rules_for_prompt()
        if tech_rules:
            context_msg += f"{tech_rules}\n\n"
        response = _chat_or_stream(
            on_token,
            model=settings.chat_model,  # strong: dead-end, open-ended reasoning
            system=DIAGNOSTIC_SYSTEM,
            messages=state.messages + [{"role": "user", "content": f"{context_msg}User said: {user_input}{_WO_DIRECTIVE if _WORK_ORDER_RE.search(user_input) else ''}"}],
        )
        state.messages.append({"role": "user", "content": user_input})
        state.messages.append({"role": "assistant", "content": response})
        return state, response

    # Use LLM to map user's answer to a child node
    _status("Interpreting response...")
    current = get_node(state.current_node_id)
    children_json = json.dumps([
        {
            "id": c.get("id", ""),
            "type": c.get("_node_type", ""),
            "title": c.get("title", ""),
            "condition": c.get("_condition", ""),
            "description": c.get("description", c.get("instruction", "")),
        }
        for c in children
    ], indent=2)

    interpret_prompt = (
        f"Current node: {current.get('title', '')} ({current.get('_node_type', '')})\n"
        f"Question that was asked: {state.messages[-1]['content'] if state.messages else ''}\n\n"
        f"User's response: {user_input}\n\n"
        f"Available child nodes:\n{children_json}"
    )

    raw = interpret(system=INTERPRET_SYSTEM, messages=[{"role": "user", "content": interpret_prompt}])

    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)
        interpretation = json.loads(cleaned)
    except json.JSONDecodeError:
        interpretation = {"matched_node_id": None, "confidence": 0, "interpretation": "Could not parse"}

    matched_id = interpretation.get("matched_node_id")

    if matched_id:
        # Traverse to the matched node
        state.current_node_id = matched_id
        matched_node = get_node(matched_id)
        state.current_node_type = matched_node.get("_node_type", "") if matched_node else ""

        state.step_order += 1
        state.steps.append({
            "step_order": state.step_order,
            "neo4j_node_id": matched_id,
            "node_type": state.current_node_type,
            "user_answer": user_input,
            "interpretation": interpretation.get("interpretation", ""),
            "confidence": interpretation.get("confidence", 0),
        })

        # Check if we've reached a Solution
        if state.current_node_type == "Solution":
            state.phase = "estimate"
            return state, _format_solution_reached(matched_node)

        # Get next level children for the next question
        next_children = get_node_children(matched_id)
        children_desc = "\n".join(
            f"- [{c.get('_node_type', '')}] {c.get('title', '')} | "
            f"Question: {c.get('question_text', c.get('instruction', ''))}"
            for c in next_children
        )

        # Proactively search for removal/access procedures when the
        # current step involves physical work (remove, measure, etc.)
        # so the LLM can include ALL prerequisite steps automatically.
        _status("Searching for procedures...")
        prob_node = get_node(state.problem_neo4j_id) if state.problem_neo4j_id else None
        prob_title = prob_node.get("title", "") if prob_node else ""
        procedure_context = _search_procedure_context(matched_node, prob_title)

        context = (
            f"We've moved to: {matched_node.get('title', '')} ({state.current_node_type})\n"
            f"Instruction: {matched_node.get('instruction', '')}\n"
            f"Available next steps:\n{children_desc}\n\n"
            f"Previous conversation:\n"
        )
        for msg in state.messages[-4:]:
            context += f"{msg['role']}: {msg['content']}\n"

        if procedure_context:
            context += f"\n\nComponent access/removal procedure from service manual:\n{procedure_context}"

        # Work order search — gather parts, specs, procedures
        if _WORK_ORDER_RE.search(user_input):
            _status("Gathering parts and procedures for work order...")
            node_title = matched_node.get("title", "")
            wo_queries = [
                f"{prob_title} {node_title} parts required OEM part number",
                f"{prob_title} {node_title} torque specification fluid capacity",
                f"{prob_title} {node_title} removal installation procedure steps",
            ]
            wo_chunks = []
            with ThreadPoolExecutor(max_workers=len(wo_queries)) as pool:
                futures = [pool.submit(search_chunks, q, None, 3) for q in wo_queries]
                for f in as_completed(futures):
                    wo_chunks.extend(f.result())
            if wo_chunks:
                wo_context = "\n\n---\n\n".join(
                    _extract_relevant_window(c["chunk_text"], f"{prob_title} {node_title} parts torque")
                    for c in wo_chunks[:6]
                )
                context += f"\n\nParts and specifications from service manual:\n{wo_context}"

            # Look up OEM part numbers from local parts catalog
            _status("Looking up parts catalog...")
            component_names = _extract_component_names(f"{prob_title} {node_title} {procedure_context}")
            vehicle = state.vehicle_info
            parts_data = get_parts_for_work_order(
                component_names,
                make=vehicle.get("make", ""),
                model=vehicle.get("model", ""),
                year=vehicle.get("year"),
            )
            if parts_data:
                context += f"\n\n{parts_data}"

        # Inject technician corrections
        tech_rules = get_rules_for_prompt()
        if tech_rules:
            context += f"\n\n{tech_rules}"

        # Strong model for work orders (need reasoning), light for graph traversal
        use_model = settings.chat_model if _WORK_ORDER_RE.search(user_input) else settings.light_model
        response = _chat_or_stream(
            on_token,
            model=use_model,
            system=DIAGNOSTIC_SYSTEM,
            messages=[
                {"role": "user", "content": f"[SERVICE MANUAL REFERENCE — not from user]:\n{context}\n\nUser said: {user_input}{_WO_DIRECTIVE if _WORK_ORDER_RE.search(user_input) else ''}"}
            ],
        )
    else:
        # No match found — user may have redirected, disagreed, or asked
        # something outside the graph tree. Search based on their actual
        # intent, not just the current node.
        _status("Searching chunks database...")

        # Use the problem context + user's actual words for search
        prob_node = get_node(state.problem_neo4j_id) if state.problem_neo4j_id else None
        prob_title = prob_node.get("title", "") if prob_node else ""
        prob_desc = prob_node.get("description", "") if prob_node else ""

        search_context = _effective_search_query(user_input, state.messages)
        search_qs = [
            search_context,
            f"{prob_title} {user_input}",
            f"{prob_desc} {user_input}",
        ]
        chunks = []
        existing_ids: set[str] = set()
        with ThreadPoolExecutor(max_workers=len(search_qs)) as pool:
            futures = [pool.submit(search_chunks, q, None, 3) for q in search_qs]
            for f in as_completed(futures):
                for rc in f.result():
                    if rc["id"] not in existing_ids:
                        chunks.append(rc)
                        existing_ids.add(rc["id"])

        # Also keyword search for DTC codes from the problem
        dtc_codes = re.findall(r'[PBCUp]\d{4}', prob_title)
        for code in dtc_codes:
            for kc in search_chunks_keyword(code, limit=2):
                if kc["id"] not in existing_ids:
                    chunks.append(kc)
                    existing_ids.add(kc["id"])

        # Also search for removal/access procedures if conversation involves physical work
        _status("Searching for procedures...")
        assistant_tail = ""
        for msg in reversed(state.messages):
            if msg["role"] == "assistant":
                assistant_tail = msg["content"][-500:]
                break
        if _ACTION_RE.search(f"{user_input} {assistant_tail}"):
            current_desc = current.get("description", "") if current else ""
            removal_q = f"{current_desc} {assistant_tail[-150:]} removal procedure disassembly valve body"
            for rc in search_chunks(removal_q, limit=5):
                if rc["id"] not in existing_ids:
                    chunks.append(rc)
                    existing_ids.add(rc["id"])

        # Work order search — gather parts, specs, procedures
        parts_context = ""
        if _WORK_ORDER_RE.search(f"{user_input} {assistant_tail}"):
            _status("Gathering parts and procedures for work order...")
            wo_queries = [
                f"{prob_desc} {prob_title} parts required OEM part number",
                f"{prob_desc} {prob_title} torque specification fluid capacity",
                f"{prob_desc} {prob_title} removal installation procedure steps",
            ]
            with ThreadPoolExecutor(max_workers=len(wo_queries)) as pool:
                futures = [pool.submit(search_chunks, q, None, 3) for q in wo_queries]
                for f in as_completed(futures):
                    for rc in f.result():
                        if rc["id"] not in existing_ids:
                            chunks.append(rc)
                            existing_ids.add(rc["id"])

            # Look up OEM part numbers from local parts catalog
            _status("Looking up parts catalog...")
            component_names = _extract_component_names(f"{prob_title} {prob_desc} {assistant_tail}")
            vehicle = state.vehicle_info
            parts_context = get_parts_for_work_order(
                component_names,
                make=vehicle.get("make", ""),
                model=vehicle.get("model", ""),
                year=vehicle.get("year"),
            )

        chunk_context = "\n\n---\n\n".join(_extract_relevant_window(c["chunk_text"], f"{user_input} {search_context}") for c in chunks[:10]) if chunks else ""
        context_msg = f"[SERVICE MANUAL REFERENCE — not from user]:\n{chunk_context}\n\n" if chunk_context else ""
        if parts_context:
            context_msg += f"{parts_context}\n\n"
        # Inject technician corrections
        tech_rules = get_rules_for_prompt()
        if tech_rules:
            context_msg += f"{tech_rules}\n\n"
        response = _chat_or_stream(
            on_token,
            model=settings.chat_model,  # strong: no match, user redirected, open-ended
            system=DIAGNOSTIC_SYSTEM,
            messages=state.messages + [{"role": "user", "content": f"{context_msg}User said: {user_input}{_WO_DIRECTIVE if _WORK_ORDER_RE.search(user_input) else ''}"}],
        )

    state.messages.append({"role": "user", "content": user_input})
    state.messages.append({"role": "assistant", "content": response})
    return state, response


def _format_solution_reached(solution: dict) -> str:
    """Format a message when a Solution node is reached."""
    title = solution.get("title", "Unknown solution")
    difficulty = solution.get("difficulty", "unknown")
    precautions = solution.get("precautions", "")
    labor = solution.get("total_labor_minutes", 0)

    msg = f"\nDiagnosis complete! The identified solution is:\n\n"
    msg += f"  {title}\n"
    msg += f"  Difficulty: {difficulty}\n"
    if labor:
        msg += f"  Estimated labor: {labor} minutes\n"
    if precautions:
        msg += f"  Precautions: {precautions}\n"
    msg += "\nGenerating a detailed repair estimate..."
    return msg
