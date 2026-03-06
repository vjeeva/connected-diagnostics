# Connected Diagnostics: Workflows Reference

> Companion to the [main architecture plan](./diagnostic_knowledge_system_c18065b2.plan.md). This document defines the high-level components and workflows. The arch plan contains the detailed schemas, data models, and implementation specifics.

---

## Core Workflow: The Diagnostic Journey

This is the spine of the entire system. Everything else exists to make this workflow better, faster, or broader.

```
Problem Presented → Diagnostic Step(s) → Root Cause Identified → Fix Step(s) → Verified → Done
```

### Phases

#### 1. Problem Presented

The user enters the system with a vehicle problem. Two entry points:

- **Conversational**: User describes the problem in natural language ("my 2019 Civic won't start"). LLM extracts vehicle + symptom, pgvector finds matching Problem nodes, Neo4j filters by vehicle.
- **DTC Input**: User provides diagnostic trouble codes via text, scan tool photo (Claude Vision OCR), or PDF report. Codes are extracted and matched to Problem nodes in the graph.

**Output**: A starting Problem node in Neo4j, linked to the user's vehicle.

#### 2. Diagnostic Steps

The system walks the user through the diagnostic tree. Each step is a cycle:

```
Present question/test → User responds → LLM maps response to graph node → Traverse to next node → Repeat
```

Node types traversed: Problem → Symptom → Test → Result → (back to Test or forward to Solution)

The LLM's role is translation only — it converts natural language to graph operations and graph data to conversational responses. The graph is the source of truth. The LLM cannot skip nodes or invent diagnoses.

**Key behaviors**:
- Alternative paths (community-contributed) are presented alongside manual paths, ranked by vote score
- Cross-car correlations surface relevant procedures from other vehicles ("this same test works on Accords too")
- Each step is recorded in `session_steps` for path analytics and session reconstruction

#### 3. Root Cause Identified

A Solution node is reached. This is the pivot point between "what's wrong" and "how to fix it."

**What happens here**:
- Estimate generation triggers (see Supporting Workflow: Estimate Generation)
- Session phase transitions from `diagnosis` → `estimate`
- The user sees the root cause + a cost/time estimate before deciding to proceed

#### 4. Fix Steps

If the user approves the estimate, the session enters the `repair` phase. The system presents an ordered sequence of Step nodes (via NEXT_STEP edges from the Solution).

Each step has:
- Detailed instructions
- Required parts and tools
- Time estimates
- Safety warnings
- Media references (diagrams, wiring schematics)

**Key behaviors**:
- Each step is tracked individually in `session_repair_steps` (pending → in_progress → completed/skipped/blocked)
- Steps can be skipped with a reason
- If a new problem is discovered during a fix step, a child session is spawned (linked via `session_links`)
- The parent session cannot complete until child sessions resolve

#### 5. Verified → Done

After all fix steps are completed or skipped:
- Session transitions to `verification` phase
- System prompts user to verify the fix worked
- On success: session → `completed`
- On failure: session → back to `repair` (or back to `diagnosis` if the root cause was wrong)

### Phase State Machine

```
diagnosis → estimate → decision → repair → verification → completed
                                      ↓                        ↑
                                  diagnosis (child)    ←   (child resolves)

Any phase → abandoned (user quits)
decision → estimate (user requests alternative solution)
verification → repair (verification failed)
```

---

## Entry Points

### Conversational Entry

User describes their problem in natural language. The system:
1. LLM extracts vehicle info + symptom description
2. pgvector semantic search finds matching Problem node IDs
3. Neo4j filters to Problems that APPLIES_TO the user's vehicle
4. LLM picks best match, presents first diagnostic question
5. Session created, core workflow begins at Phase 2

### DTC Entry

User provides diagnostic trouble codes. The system:
1. Extract codes via the appropriate handler:
   - Text: regex + LLM for context
   - Image: Claude Vision OCR
   - PDF: text extraction + regex + LLM
2. Match codes to Problem nodes in Neo4j (via `dtc_codes` property)
3. If multiple codes found, present grouped by likely root cause
4. User confirms which problem to diagnose first
5. Session created, core workflow begins at Phase 2

---

## Supporting Workflows

### 1. Manual Ingestion

**Purpose**: Feeds the diagnostic graph that the core workflow traverses.

**Trigger**: Admin ingests a new service manual (PDF).

**Flow**:
```
PDF → Parse → Chunk by section → LLM extracts diagnostic structure + captures references
→ Resolve references (figures, diagrams, cross-refs) → Vision processes diagrams
→ Create Neo4j nodes and edges → Store chunks + embeddings in PostgreSQL
→ Link to Vehicle nodes via APPLIES_TO → Trigger correlation (see below)
```

**Output**: New Problem/Symptom/Test/Result/Solution/Step/Part/Tool nodes in Neo4j, searchable chunks in pgvector.

**Post-ingestion**: Correlation engine runs on all new nodes to find cross-vehicle similarities.

---

### 2. Correlation Engine

**Purpose**: Finds similarities across vehicles at every level of the diagnostic journey — problems, diagnostic steps, fix steps, and root causes. This is the cross-car intelligence layer that makes the knowledge base exponentially more valuable as more vehicles are added.

**What gets correlated**:
- **Problems**: Same issue manifests on different vehicles (e.g., "Engine Won't Start" on Civic ≈ same on Accord)
- **Diagnostic Steps**: Same test applies across vehicles (e.g., "Check battery voltage" is universal)
- **Fix Steps**: Same repair procedure works on different cars (e.g., battery replacement steps are nearly identical across many vehicles)
- **Root Causes**: Same underlying cause regardless of make/model (e.g., dead battery, failed MAF sensor)

**Discovery Channels** (all system-driven, all active from Phase 1):

1. **Ingestion-Time Comparison**: When a new manual is ingested, every new Solution/Step node's embedding is compared against existing nodes on other vehicles. High-similarity matches (>0.90) generate candidates.

2. **Session-Driven Detection**: When a diagnostic session completes, the full path (problem → solution) is compared against completed sessions on different vehicles. Same Problem→Solution pattern across vehicles = candidate.

3. **Batch Embedding Sweep**: Periodic background job checks nodes that haven't been compared yet. Catches anything the other channels missed.

The system discovers all correlations. Humans validate them — they never need to find or submit correlations themselves.

**Candidate Pipeline**:
```
System discovers similarity (any channel) → Candidate created in cross_car_candidates
→ Review queue (side-by-side comparison of the two nodes + vehicle context)
→ Reviewer validates: SIMILAR_TO (soft link) / SHARED_PROCEDURE (hard link) / Reject
→ Approved: Neo4j edge created, reviewer earns reputation
```

**Relationship types created**:
- `SIMILAR_TO` — soft: "these are related" (e.g., similar diagnostic approach)
- `SHARED_PROCEDURE` — hard: "these are literally the same procedure"

**Impact on core workflow**: When a user is in the diagnostic journey, correlated paths from other vehicles surface as additional options. "This same test resolved the issue on 14 other vehicles."

---

### 3. Estimate Generation

**Purpose**: Compiles a cost/time estimate at the pivot point between "root cause identified" and "fix steps."

**Trigger**: Solution node reached in the core workflow.

**Flow**:
```
Fetch Solution + NEXT_STEP chain + REQUIRES_PART + REQUIRES_TOOL from Neo4j
→ Parallel parts lookups via Parts Service (Amazon, eBay, internal catalog)
→ Quality ranking (Tier 1-4) on all results
→ Select best price per part (cheapest Tier 1, fallback Tier 2, fallback manual price)
→ Calculate labor (total_labor_minutes × labor_rate)
→ LLM formats conversational summary
→ Freeze as RepairEstimate snapshot in session_estimates
→ Session phase: diagnosis → estimate
```

**Parts Service**: Plugin architecture querying multiple providers in parallel. Each provider implements a standard interface. Results are merged, deduped, and quality-ranked.

**Quality Tiers**:
- Tier 1: OEM Genuine (default estimate price)
- Tier 2: Known Quality Aftermarket (shown as alternative)
- Tier 3: Unverified Aftermarket (shown with warning)
- Tier 4: Excluded (hidden from customers)

**Estimate is immutable**: Re-generating always creates a new row with fresh prices. Old estimates are never mutated.

---

### 4. Contribution Pipeline

**Purpose**: Technicians enrich the graph that the core workflow traverses.

**Contribution types**:
- Add new diagnostic nodes (Problem, Test, Solution, Step)
- Add alternative paths (ALTERNATIVE edge to existing node)
- Annotate existing nodes (notes, tips, warnings)
- Attach media (photos, diagrams → S3)
- Update cost/time estimates

**Routing** (governed by `TRUST_MODE` config):

| Trust Mode | Standard users | Trusted users | Expert/Admin |
|------------|---------------|---------------|--------------|
| Bootstrap | Rejected (invite-only) | Publish directly | Publish directly |
| Hybrid | Review queue (2 approvals) | Publish directly | Publish directly |
| Reputation | Full tier-based logic | Tier-based logic | Publish directly |

**Note**: Cross-car correlation validation is a separate activity from contributions — it lives in the Correlation Engine's review queue (see above). Techs with sufficient trust level can validate candidates; expert/admin can approve grey-area cases directly. Validation counts toward contribution quota but is not routed through the contribution pipeline.

**Post-publish pipeline**:
```
Publish → Award reputation (+10)
→ Generate embeddings (if new text content) → Insert into manual_chunks
→ Trigger correlation (if Solution or Step node) → Check for cross-car matches
```

---

### 5. Vote & Reputation Sync

**Purpose**: Ranks the paths the core workflow presents. Maintains trust tiers that govern the contribution pipeline.

**Vote flow**:
```
User votes on a Neo4j node (via PostgreSQL votes table)
→ Background task aggregates score
→ Writes vote_score back to Neo4j node property
→ Eventually consistent (seconds, not minutes)
```

**Reputation effects**:
- +5 per upvote received, -2 per downvote
- +10 contribution approved, -10 contribution rejected
- +15 alternative path chosen by user completing diagnosis
- +25 cross-car correlation validated

**Trust level transitions**: standard → trusted (20+ rep in hybrid, 100+ in reputation) → expert (500+ rep)

**Impact on core workflow**: vote_score on nodes influences path ranking in Cypher traversal queries. Higher-voted paths are presented first.

---

## Components Summary

| Component | What it does | Data stores |
|-----------|-------------|-------------|
| **Diagnostic Engine** | Orchestrates the per-turn loop: LLM → graph → LLM → response | Neo4j, PostgreSQL, LLM |
| **Semantic Search** | pgvector similarity search for problem matching | PostgreSQL (pgvector) |
| **Parts Service** | Queries providers in parallel, merges, quality-ranks | Neo4j (Part nodes), PostgreSQL (brand lists, shop settings), External APIs |
| **Estimate Service** | Compiles Solution → parts + labor → frozen snapshot | Neo4j, PostgreSQL, Parts Service, LLM |
| **Session Manager** | Phase transitions, linked sessions, repair step tracking | PostgreSQL |
| **Contribution Service** | Trust-gated routing, review queue, post-publish pipeline | Neo4j, PostgreSQL |
| **Correlation Engine** | Cross-vehicle similarity detection across all node types | Neo4j, PostgreSQL (pgvector), LLM |
| **Ingestion Pipeline** | PDF → graph + embeddings | Neo4j, PostgreSQL, S3, LLM, Vision |
| **DTC Processors** | Text/Image/PDF → extracted codes | LLM, Vision |
| **Sync Service** | Vote scores, reputation → Neo4j node properties | Neo4j, PostgreSQL |
| **Trust Controller** | TRUST_MODE config, routing rules, tier transitions | PostgreSQL (config) |

---

## Open Issues

None — all issues from arch plan review passes 1-8 have been resolved.
