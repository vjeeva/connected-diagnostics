---
name: Diagnostic Knowledge System
overview: Design and build Connected Diagnostics -- a diagnostic knowledge base starting with automotive repair. Neo4j is the primary store for the diagnostic graph (nodes, edges, cross-car relationships). PostgreSQL + pgvector handles relational data (users, sessions, pricing) and semantic search. Technicians contribute knowledge through a reputation-gated system.
todos:
  - id: setup-neo4j
    content: Set up Neo4j with APOC plugin. Define node labels (Problem, Symptom, Test, Result, Solution, Step, Vehicle, System, Component) and relationship types (LEADS_TO, CONFIRMS, ALTERNATIVE, REQUIRES, APPLIES_TO, SIMILAR_TO, SHARED_PROCEDURE). Seed with initial schema constraints and indexes.
    status: pending
  - id: setup-postgres
    content: "Set up PostgreSQL with pgvector extension. Create relational schema: users (with reputation), manual_chunks (with embeddings), diagnostic_sessions, contributions, votes, pricing_data. Run Alembic migrations."
    status: pending
  - id: setup-backend
    content: "Scaffold FastAPI backend with dual-DB architecture: neomodel or neo4j-driver for graph ops, SQLAlchemy + asyncpg for relational/vector ops. Set up the graph_service and search_service abstraction layers."
    status: pending
  - id: pdf-ingestion
    content: "Build PDF ingestion pipeline: parse PDF -> chunk by section -> LLM structured extraction -> create Neo4j nodes and edges -> generate embeddings -> store chunks in PostgreSQL."
    status: pending
  - id: diagnostic-engine
    content: "Build diagnostic engine: Neo4j Cypher traversal for walking the tree, pgvector similarity search for problem matching, LLM for natural language interpretation. Implement the contribution-aware path ranking."
    status: pending
  - id: contribution-system
    content: "Build technician contribution system: reputation model, contribution types (new nodes, alternative paths, annotations, attachments), review queue, voting, auto-promotion logic."
    status: pending
  - id: frontend-mvp
    content: Build Next.js app with both customer diagnostic flow AND a basic technician contribution interface for adding/annotating nodes.
    status: pending
  - id: docker-compose
    content: "Create Docker Compose: Neo4j, PostgreSQL + pgvector, Redis, FastAPI backend, Next.js frontend."
    status: pending
isProject: false
---

# Connected Diagnostics: System Architecture (v2)

## Revision Notes

v2 changes from v1:

- **Neo4j is now a foundational component**, not deferred. Cross-car knowledge sharing and rich graph traversal are core to the product.
- **Technician contribution system** is fully designed and built in Phase 1, not deferred to Phase 2.
- **Dual-database architecture**: Neo4j owns the diagnostic graph, PostgreSQL + pgvector owns relational data and semantic search.

---

## Why Two Databases: Neo4j + PostgreSQL

Each database does what it's best at:

**Neo4j (graph database) owns:**

- The entire diagnostic tree (nodes and edges)
- Cross-car relationships ("this alternator replacement procedure is the same across all 2018-2022 Honda Civics")
- "SIMILAR_TO" links between procedures across different vehicles
- "SHARED_PROCEDURE" links where identical steps apply to multiple cars
- Graph traversal queries ("walk me from problem -> diagnosis in 3 hops")
- Relationship-heavy queries ("what other cars have this exact same issue?")

**PostgreSQL + pgvector owns:**

- Users, authentication, reputation scores
- Manual chunks with vector embeddings (for semantic search / RAG)
- Diagnostic session logs (relational with JSONB)
- Pricing data (parts costs, labor rates, regional data)
- Contribution audit trail
- Vote records

**Why not just Neo4j for everything?** Neo4j is poor at full-text/vector similarity search, user authentication patterns, and transactional writes like voting tallies. **Why not just PostgreSQL?** Recursive CTEs for graph traversal get unwieldy fast once you need cross-car links, weighted path scoring, and variable-depth traversal. When a tech says "this fix also works on Accords," that's a `SIMILAR_TO` edge in Neo4j -- trivial. In SQL, it's a mess of junction tables.

---

## Neo4j Graph Schema

```mermaid
graph LR
    subgraph nodeLabels [Node Labels]
        Problem["Problem"]
        Symptom["Symptom"]
        TestNode["Test"]
        Result["Result"]
        Solution["Solution"]
        Step["Step"]
        Vehicle["Vehicle"]
        SystemNode["System<br/>(Engine, Brakes, etc.)"]
        Component["Component<br/>(Alternator, Battery, etc.)"]
    end

    subgraph relTypes [Relationship Types]
        R1["LEADS_TO<br/>{condition, confidence, display_order}"]
        R2["CONFIRMS<br/>{condition}"]
        R3["ALTERNATIVE<br/>{contributor_id, vote_score}"]
        R4["REQUIRES<br/>{tool, part_number}"]
        R5["APPLIES_TO<br/>{year_start, year_end, trim}"]
        R6["SIMILAR_TO<br/>{similarity_score, contributor_id}"]
        R7["SHARED_PROCEDURE<br/>{verified}"]
        R8["BELONGS_TO<br/>{}"]
        R9["HAS_COMPONENT<br/>{}"]
    end
```



**Cypher example -- a diagnostic node and its cross-car links:**

```cypher
// Create a problem node
CREATE (p:Problem {
  id: randomUUID(),
  title: "Engine Won't Start",
  description: "Vehicle fails to start when ignition is turned",
  source_type: "manual",
  source_ref: "Honda Civic 2019 Service Manual p.412",
  created_at: datetime(),
  vote_score: 0
})

// Link it to a vehicle
MATCH (p:Problem {title: "Engine Won't Start"})
MATCH (v:Vehicle {make: "Honda", model: "Civic", year: 2019})
CREATE (p)-[:APPLIES_TO {year_start: 2016, year_end: 2021}]->(v)

// Link a shared procedure across cars
MATCH (sol1:Solution {title: "Replace Battery"})
MATCH (sol2:Solution {title: "Replace Battery - Accord"})
CREATE (sol1)-[:SHARED_PROCEDURE {verified: true}]->(sol2)

// Traversal: walk a diagnostic path
MATCH path = (p:Problem {title: "Engine Won't Start"})
  -[:LEADS_TO*1..6]->(sol:Solution)
WHERE ALL(r IN relationships(path) WHERE r.vehicle_id IS NULL
  OR r.vehicle_id = $vehicleId)
RETURN path
ORDER BY reduce(s = 0, r IN relationships(path) | s + coalesce(r.vote_score, 0)) DESC
```

**Key graph design decisions:**

- **Vehicle is a node, not a property.** This lets you query "show me all problems that affect both Civic and Accord" as a graph pattern match, not a table join.
- **System and Component are separate node types.** A Vehicle HAS_COMPONENT Battery. Battery BELONGS_TO Electrical System. This creates a taxonomy you can traverse: "show me all electrical problems for this car."
- **ALTERNATIVE edges are the contribution mechanism.** When a tech says "there's a better way to do this step," that creates an ALTERNATIVE edge from the existing node to a new node. The original manual path stays intact; the community path lives alongside it.
- **SIMILAR_TO and SHARED_PROCEDURE** are the cross-car magic. SIMILAR_TO is soft ("these are related"), SHARED_PROCEDURE is hard ("these are literally the same procedure").

---

## Technician Contribution System

This is the engine that makes the knowledge base grow. The model is inspired by Stack Overflow's reputation system but adapted for diagnostic knowledge.

### Bootstrap-Friendly Trust Model

The trust model adapts to the size of the active community. A config flag `TRUST_MODE` (`bootstrap`, `hybrid`, or `reputation`) controls which phase is active. Transitions between phases are a config change, not a schema migration.

#### Phase A: Bootstrap Mode (fewer than ~50 active technicians)

- The admin manually invites the first 10-20 technicians. Invited users get **Trusted** status immediately -- no earning required.
- Trusted users can contribute directly (no review queue). All contributions are visible to all other Trusted users.
- Any Trusted user can flag a contribution as questionable. Flags go to the admin.
- Voting still happens, but it ranks content rather than gating publish.
- The admin can revoke Trusted status if someone contributes garbage.
- This is a **high-trust small team** model -- like a shared Google Doc among colleagues.

#### Phase B: Hybrid Mode (50-500 active technicians)

- New signups who are NOT invited start as **Standard** users.
- Standard users' contributions go through lightweight review: any 2 Trusted users approve = published.
- Trusted users still publish directly.
- Reputation points start accumulating for everyone, but thresholds are low: 20 rep to become Trusted (achievable in a week of active use).
- The admin can still manually grant Trusted status (e.g., a known master tech joins).

#### Phase C: Full Reputation Mode (500+ active technicians)

- The full 4-tier system activates with adjusted thresholds.
- All existing Trusted users get their accumulated rep mapped to the appropriate tier.
- Review queue is now staffed by hundreds of Tier 2+ users -- it works at scale.

```
Trust Levels:
  standard  - Default for new signups (non-invited)
  trusted   - Invited users (bootstrap) or earned via reputation (hybrid/full)
  expert    - Earned at 500+ rep (full mode) or admin-granted
  admin     - Platform administrators

Trust Sources:
  invited       - Admin invited this user (gets trusted immediately)
  earned        - Reputation threshold crossed automatically
  admin_granted - Admin manually promoted this user

Reputation is earned:
  +10  Contribution approved by reviewer
  +5   Contribution upvoted
  +15  Your alternative path is chosen by a user completing diagnosis
  +25  You create a cross-car link that gets verified
  -2   Contribution downvoted
  -10  Contribution rejected by reviewer
```

#### Why This Works at 10 Users

- Day 1: You invite 10 techs. They all have Trusted status.
- Day 2: They start adding knowledge. No queue. No waiting. No friction.
- Week 2: You see who's active, who contributes good stuff. The knowledge base is growing.
- Month 3: Word spreads. New techs sign up organically. They start in Standard tier. Your original 10 review their stuff.
- Month 6+: You have 100+ users. Switch to hybrid mode. Reputation matters more.
- Year 1: Full reputation system online.

### Contribution Types

```mermaid
flowchart TD
    Tech["Technician"]

    Tech --> NewNode["Add New Node"]
    Tech --> AltPath["Add Alternative Path"]
    Tech --> Annotate["Annotate Existing Node"]
    Tech --> Attach["Attach Media"]
    Tech --> CrossCar["Link Across Cars"]
    Tech --> CostUpdate["Update Cost/Time"]

    NewNode --> ReviewQ{"Review Queue - standard users"}
    NewNode --> Direct{"Direct Publish - trusted/expert/admin"}
    AltPath --> ReviewQ
    AltPath --> Direct
    Annotate --> Direct2{"Direct Publish - trusted+"}
    Attach --> Direct2
    CrossCar --> ReviewQ2{"Review Queue - standard/trusted"}
    CrossCar --> Direct3{"Direct Publish - expert/admin"}
    CostUpdate --> Direct2
```



### How a Contribution Flows

```mermaid
sequenceDiagram
    participant T as Technician
    participant App as Web/Voice
    participant API as Backend
    participant Neo as Neo4j
    participant PG as PostgreSQL

    T->>App: "I have a faster way to test the alternator on Civics"
    App->>API: POST /contribute {type: "alternative", target_node_id, vehicle_id, content}

    API->>PG: Check user trust_level and TRUST_MODE config
    PG-->>API: trust_level: trusted, TRUST_MODE: bootstrap

    alt TRUST_MODE=bootstrap AND trust_level in (trusted, expert, admin)
        API->>Neo: Create node + ALTERNATIVE edge (status: published)
        API->>PG: Create contribution record (status: published), log audit
        API-->>App: "Published! Other technicians can now see your approach."
    else TRUST_MODE=hybrid AND trust_level=standard
        API->>PG: Create contribution record (status: pending_review)
        API->>Neo: Create draft node + ALTERNATIVE edge (status: draft)
        API-->>App: "Submitted for review. Two trusted users need to approve."
        Note over Neo,PG: Later, 2 trusted users approve...
        API->>Neo: Set node + edge status to published
        API->>PG: Update contribution status, award +10 rep
    else TRUST_MODE=reputation
        Note over API: Apply full tier-based logic
    end
```

The contribution routing logic in pseudocode:

```
Contribution arrives:
  if TRUST_MODE == bootstrap:
    if user.trust_level in (trusted, expert, admin): publish directly
    else: reject (bootstrap mode is invite-only)
  elif TRUST_MODE == hybrid:
    if user.trust_level in (trusted, expert, admin): publish directly
    else: send to review (need 2 trusted approvals)
  elif TRUST_MODE == reputation:
    apply full tier-based logic (standard thresholds)
```



### What the Contribution Looks Like in the Graph

Before a technician contributes:

```mermaid
flowchart LR
    T1["TEST: Check alternator output - manual method"]
    R1["RESULT: Below 13.5V"]
    SOL["SOLUTION: Replace alternator"]

    T1 -->|"LEADS_TO, source: manual"| R1
    R1 -->|"LEADS_TO"| SOL
```

After a technician adds an alternative:

```mermaid
flowchart LR
    T1["TEST: Check alternator output - manual method"]
    T2["TEST: Quick alternator check - community method, 14 votes"]
    R1["RESULT: Below 13.5V"]
    R2["RESULT: Engine dies or RPM drops"]
    SOL["SOLUTION: Replace alternator"]

    T1 -->|"LEADS_TO, source: manual"| R1
    T2 -->|"ALTERNATIVE, vote_score: 14"| T1
    T2 -->|"LEADS_TO"| R2
    R1 -->|"LEADS_TO"| SOL
    R2 -->|"LEADS_TO"| SOL
```



The ALTERNATIVE edge connects the community-contributed test to the original manual test. Users see both options, ranked by vote score. The manual path is always preserved as the "official" baseline.

---

## PostgreSQL Schema (Relational + Vector)

PostgreSQL handles everything that isn't the diagnostic graph itself:

```sql
-- Users and reputation
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    user_type TEXT NOT NULL CHECK (user_type IN ('customer', 'technician', 'admin')),
    reputation INT DEFAULT 0,
    trust_level TEXT NOT NULL DEFAULT 'standard'
        CHECK (trust_level IN ('standard', 'trusted', 'expert', 'admin')),
    trust_source TEXT NOT NULL DEFAULT 'earned'
        CHECK (trust_source IN ('invited', 'earned', 'admin_granted')),
    specializations JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Manual chunks for RAG / semantic search
CREATE TABLE manual_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_neo4j_id TEXT NOT NULL,       -- references Neo4j Vehicle node
    source_file TEXT NOT NULL,
    page_number INT,
    chunk_text TEXT NOT NULL,
    chunk_type TEXT NOT NULL,              -- procedure, diagram, parts_list, spec, warning
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON manual_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Contributions audit trail
CREATE TABLE contributions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    contribution_type TEXT NOT NULL,       -- new_node, alternative, annotation, attachment, cross_car_link, cost_update
    target_neo4j_node_id TEXT,            -- the node being modified/extended
    created_neo4j_node_id TEXT,           -- the new node created (if any)
    content JSONB NOT NULL,               -- full contribution payload
    status TEXT DEFAULT 'pending_review',  -- pending_review, published, rejected, superseded
    reviewed_by UUID REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    review_notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Votes on Neo4j nodes (referenced by neo4j ID)
CREATE TABLE votes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    neo4j_node_id TEXT NOT NULL,
    vote_value INT NOT NULL CHECK (vote_value IN (-1, 1)),
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, neo4j_node_id)
);

-- Diagnostic sessions
CREATE TABLE diagnostic_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    vehicle_neo4j_id TEXT NOT NULL,
    starting_problem_neo4j_id TEXT NOT NULL,
    steps_taken JSONB DEFAULT '[]',       -- [{node_id, answer, timestamp}, ...]
    final_diagnosis_neo4j_id TEXT,
    chosen_path_neo4j_ids TEXT[],         -- ordered list of node IDs in the path taken
    status TEXT DEFAULT 'in_progress',
    created_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- Pricing data (crowdsourced + manual)
CREATE TABLE pricing_data (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    neo4j_solution_id TEXT NOT NULL,
    vehicle_neo4j_id TEXT,
    region TEXT,                           -- zip code prefix or metro area
    parts_cost_low DECIMAL,
    parts_cost_high DECIMAL,
    labor_cost_low DECIMAL,
    labor_cost_high DECIMAL,
    labor_hours_est DECIMAL,
    source TEXT NOT NULL,                  -- manual, technician, aggregated
    reported_by UUID REFERENCES users(id),
    reported_at TIMESTAMPTZ DEFAULT now()
);
```

---

## Dual-Database Sync Pattern

The two databases reference each other by Neo4j node IDs stored as text fields in PostgreSQL. This is intentionally loose coupling.

```mermaid
flowchart LR
    subgraph neo [Neo4j]
        Node["DiagnosticNode<br/>id: 'abc-123'<br/>title: 'Replace Battery'"]
    end

    subgraph pg [PostgreSQL]
        Vote["votes<br/>neo4j_node_id: 'abc-123'<br/>vote_value: +1"]
        Contrib["contributions<br/>target_neo4j_node_id: 'abc-123'"]
        Session["diagnostic_sessions<br/>steps_taken: ['abc-123', ...]"]
    end

    Node -.-|"referenced by ID"| Vote
    Node -.-|"referenced by ID"| Contrib
    Node -.-|"referenced by ID"| Session
```



**Vote score sync:** When a vote is cast in PostgreSQL, a background task aggregates the score and writes it back to the Neo4j node's `vote_score` property. This is eventually consistent (seconds, not minutes) and avoids Neo4j write contention.

---

## Updated Architecture

```mermaid
flowchart TB
    subgraph clients [Client Interfaces]
        CustWeb["Customer Web App<br/>(Next.js)"]
        TechPortal["Technician Portal<br/>(Next.js)"]
        Voice["Voice Interface<br/>(Phase 3)"]
    end

    subgraph api [API Layer]
        Gateway["FastAPI Backend"]
    end

    subgraph services [Core Services]
        DiagEngine["Diagnostic Engine<br/>(Neo4j traversal + LLM)"]
        SearchSvc["Semantic Search<br/>(pgvector)"]
        ContribSvc["Contribution Service<br/>(review, reputation, sync)"]
        IngestionSvc["Manual Ingestion<br/>Pipeline"]
        PricingSvc["Repair Pricing<br/>Engine"]
    end

    subgraph ai [AI Layer]
        LLM["LLM API<br/>(Claude)"]
        Embeddings["Embedding Model"]
    end

    subgraph data [Data Layer]
        Neo["Neo4j<br/>Diagnostic Graph"]
        PG["PostgreSQL + pgvector<br/>Users, Search, Sessions"]
        S3["Object Storage<br/>PDFs, images"]
        Cache["Redis<br/>Sessions, vote aggregation"]
    end

    CustWeb --> Gateway
    TechPortal --> Gateway
    Voice --> Gateway

    Gateway --> DiagEngine
    Gateway --> SearchSvc
    Gateway --> ContribSvc
    Gateway --> PricingSvc

    DiagEngine --> LLM
    DiagEngine --> Neo
    DiagEngine --> PG
    SearchSvc --> Embeddings
    SearchSvc --> PG
    ContribSvc --> Neo
    ContribSvc --> PG
    ContribSvc --> Cache
    IngestionSvc --> LLM
    IngestionSvc --> Embeddings
    IngestionSvc --> Neo
    IngestionSvc --> PG
    IngestionSvc --> S3
    PricingSvc --> PG
```



---

## How the Diagnostic Decision Tree Works

Example tree for "Engine Won't Start" (unchanged from v1, but now natively stored as Neo4j graph):

```mermaid
flowchart TD
    P1["PROBLEM<br/>Engine Won't Start"]
    S1["SYMPTOM<br/>Clicking when turning key?"]
    S2["SYMPTOM<br/>No sound at all?"]
    S3["SYMPTOM<br/>Cranks but won't fire?"]

    T1["TEST<br/>Check battery voltage"]
    T2["TEST<br/>Check battery terminals"]
    T3["TEST<br/>Check fuse box"]
    T4["TEST<br/>Check fuel pressure"]
    T5["TEST<br/>Check spark plugs"]

    R1["RESULT<br/>Below 12V"]
    R2["RESULT<br/>Above 12V"]
    R3["RESULT<br/>Corroded terminals"]
    R4["RESULT<br/>Blown fuse"]

    SOL1["SOLUTION<br/>Replace battery<br/>$150-$300 | 30min"]
    SOL2["SOLUTION<br/>Clean terminals<br/>$20-$50 | 15min"]
    SOL3["SOLUTION<br/>Replace starter motor<br/>$300-$600 | 2hr"]
    SOL4["SOLUTION<br/>Replace fuse<br/>$5-$15 | 10min"]

    P1 --> S1
    P1 --> S2
    P1 --> S3

    S1 --> T1
    S1 --> T2

    T1 --> R1 --> SOL1
    T1 --> R2 --> T1b["TEST<br/>Check starter motor"]
    T1b --> SOL3

    T2 --> R3 --> SOL2

    S2 --> T3
    T3 --> R4 --> SOL4

    S3 --> T4
    S3 --> T5
```



The customer walks through this interactively. The LLM helps interpret natural language ("it makes a clicking noise") into the right symptom branch. Neo4j handles the traversal natively with Cypher.

---

## PDF Manual Ingestion Pipeline

Ingestion now writes to both databases:

```mermaid
flowchart LR
    PDF["PDF Service Manual"]
    Parse["1. Parse PDF"]
    Chunk["2. Chunk by Section"]
    Extract["3. LLM Extraction"]
    Neo["4a. Neo4j: Create Nodes + Edges"]
    PG["4b. PostgreSQL: Store Chunks"]
    Link["5a. Link Vehicle via APPLIES_TO"]
    Embed["5b. Generate Embeddings"]

    PDF --> Parse --> Chunk --> Extract
    Extract --> Neo --> Link
    Extract --> PG --> Embed
```



---

## Customer-Facing MVP Flow (unchanged)

```mermaid
sequenceDiagram
    participant C as Customer
    participant App as Web App
    participant API as Backend
    participant LLM as LLM
    participant Neo as Neo4j
    participant PG as PostgreSQL

    C->>App: "My 2019 Honda Civic won't start"
    App->>API: POST /diagnose {vehicle, description}
    API->>PG: Vector search on description embedding
    PG-->>API: Top matching problem node IDs
    API->>Neo: Fetch problem nodes by ID
    API->>LLM: Rank/filter matches given context
    LLM-->>API: Best matching problem + first question
    API-->>App: "Does it make a clicking sound?"

    C->>App: "Yes, clicking"
    App->>API: POST /diagnose/session/{id}/answer
    API->>Neo: Cypher traversal to next node
    Neo-->>API: Next diagnostic step
    API-->>App: "Check battery voltage with multimeter"

    Note over C,PG: ...continues through tree...

    API-->>App: Diagnosis result with cost estimate
```



---

## Tech Stack (updated)

- **Frontend**: Next.js 14 + TypeScript + Tailwind
- **Backend**: Python + FastAPI
- **Graph Database**: Neo4j 5 (diagnostic tree, cross-car relationships)
- **Relational + Vector DB**: PostgreSQL 16 + pgvector (users, sessions, search, pricing)
- **Cache**: Redis (session state, vote aggregation buffer)
- **Object Storage**: S3 or MinIO (PDFs, images, schematics)
- **AI - LLM**: Claude API (Anthropic)
- **AI - Embeddings**: OpenAI text-embedding-3-small (1536 dims)
- **Deployment**: Docker Compose (dev), AWS ECS or Railway (prod)

---

## Project Structure (updated)

```
connected-diagnostics/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/routes/
│   │   │   ├── diagnose.py          # Customer diagnostic flow
│   │   │   ├── contribute.py        # Technician contributions
│   │   │   ├── vehicles.py          # Vehicle CRUD
│   │   │   ├── nodes.py             # Diagnostic node browsing
│   │   │   ├── search.py            # Semantic search
│   │   │   ├── review.py            # Contribution review queue
│   │   │   └── auth.py              # Registration, login
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   └── security.py
│   │   ├── db/
│   │   │   ├── neo4j_client.py      # Neo4j connection + helpers
│   │   │   ├── postgres.py          # SQLAlchemy async engine
│   │   │   └── redis_client.py      # Redis connection
│   │   ├── models/                  # SQLAlchemy models (PostgreSQL)
│   │   │   ├── user.py
│   │   │   ├── manual_chunk.py
│   │   │   ├── contribution.py
│   │   │   ├── vote.py
│   │   │   ├── session.py
│   │   │   └── pricing.py
│   │   ├── graph/                   # Neo4j graph operations
│   │   │   ├── schema.py            # Cypher for constraints/indexes
│   │   │   ├── queries.py           # Reusable Cypher query templates
│   │   │   ├── traversal.py         # Diagnostic path traversal logic
│   │   │   └── mutations.py         # Node/edge creation and updates
│   │   ├── services/
│   │   │   ├── diagnostic_engine.py # Orchestrates Neo4j + LLM
│   │   │   ├── search_service.py    # pgvector similarity search
│   │   │   ├── contribution_service.py  # Contribution + reputation logic
│   │   │   ├── review_service.py    # Review queue management
│   │   │   ├── pricing_service.py   # Cost estimation
│   │   │   ├── sync_service.py      # Neo4j <-> PostgreSQL sync (votes, etc.)
│   │   │   └── llm_service.py       # LLM integration
│   │   └── ingestion/
│   │       ├── pdf_parser.py
│   │       ├── chunk_processor.py
│   │       ├── llm_extractor.py
│   │       └── graph_builder.py     # Creates Neo4j nodes from extracted data
│   ├── alembic/
│   ├── tests/
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/
│   ├── app/
│   │   ├── page.tsx                 # Landing page
│   │   ├── diagnose/                # Customer flow
│   │   ├── contribute/              # Technician contribution UI
│   │   ├── review/                  # Review queue (Tier 2+)
│   │   └── browse/                  # Browse the diagnostic tree
│   ├── components/
│   │   ├── DiagnosticChat.tsx
│   │   ├── TreeBrowser.tsx          # Visual tree navigator
│   │   ├── ContributionForm.tsx     # Submit new knowledge
│   │   ├── ReviewCard.tsx           # Review pending contributions
│   │   ├── ReputationBadge.tsx
│   │   └── PriceEstimate.tsx
│   └── package.json
├── docker-compose.yml               # Neo4j + PostgreSQL + Redis + apps
└── README.md
```

---

## Phased Roadmap (revised)

### Phase 1: Foundation + Dual MVP (Weeks 1-8)

- Set up Neo4j + PostgreSQL + pgvector + Redis via Docker Compose
- Build PDF ingestion pipeline (your car's service manual)
- Create diagnostic graph from extracted content
- Build customer web app (diagnostic chat flow)
- Build basic technician contribution interface (add nodes, alternatives, annotations)
- Implement reputation system and review queue
- Basic cost estimation

### Phase 2: Growth + Multi-Car (Weeks 9-14)

- Ingest additional vehicle manuals
- Cross-car linking (SIMILAR_TO, SHARED_PROCEDURE)
- OBD-II error code database integration
- Enhanced technician dashboard (stats, contribution history)
- Pricing crowdsourcing from technicians

### Phase 3: Voice Interface (Weeks 15-20)

- Deepgram STT + ElevenLabs TTS integration
- Conversational state machine over the Neo4j graph
- Hands-free diagnostic guidance
- Lapel mic hardware integration

### Phase 4: Scale + Intelligence (Weeks 21+)

- Auto-suggest cross-car links from embedding similarity
- ML-based path ranking (which diagnostic path resolves fastest)
- Mobile app for in-shop use
- API for third-party integrations (shop management software)

---

## Key Architectural Decisions

1. **Neo4j from day one.** Cross-car knowledge sharing is the moat. Building it on SQL and migrating later would mean rewriting every query, every API contract, and every traversal algorithm. Neo4j's Cypher makes graph patterns trivial: `MATCH (p:Problem)-[:APPLIES_TO]->(v1:Vehicle), (p)-[:APPLIES_TO]->(v2:Vehicle) WHERE v1.make = 'Honda' AND v2.make = 'Toyota' RETURN p` -- try that in SQL.
2. **Bootstrap-friendly trust model (3-phase).** A pure reputation system doesn't work when you have 10 technicians -- nobody has enough rep to review anyone else, and everything stalls. Instead, the system starts in *bootstrap mode* where invited technicians publish directly (high trust, low friction). As the team grows to ~50+, it transitions to *hybrid mode* with lightweight reviews for new users and lower thresholds. Only at scale (500+ contributors) does the full reputation-tier system activate. The `TRUST_MODE` config (`bootstrap | hybrid | reputation`) controls routing without code changes. This prevents the cold-start problem while preserving the path to full community governance.
3. **ALTERNATIVE edges, not overwrites.** Technician knowledge never replaces manual knowledge -- it lives alongside it as a parallel path. This preserves the authoritative baseline while letting community wisdom surface through voting. A customer sees "Manual method: X (verified) | Community shortcut: Y (+14 votes)".
4. **Loose coupling between databases.** PostgreSQL references Neo4j by string IDs, not foreign keys. This means either database can be replaced, scaled, or rebuilt independently. The sync service handles vote score propagation as an async background task.
5. **Embeddings in PostgreSQL, graph in Neo4j.** Semantic search ("my car shakes at highway speed") hits pgvector to find relevant problem node IDs. Those IDs then feed into Neo4j traversal. This separation means each database handles the queries it's optimized for.

