"""Semantic search over manual chunks using pgvector."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from backend.app.core.config import settings

_embed_client = None
_engine = None
_embedding_cache: dict[str, list[float]] = {}


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.postgres_sync_url, pool_size=5, pool_pre_ping=True)
    return _engine


def _get_embed_client():
    global _embed_client
    if _embed_client is None:
        if settings.embedding_provider == "openai":
            import openai
            _embed_client = openai.OpenAI(api_key=settings.openai_api_key)
        elif settings.embedding_provider == "google":
            import google.generativeai as genai
            genai.configure(api_key=settings.google_api_key)
            _embed_client = genai
        else:
            raise ValueError(f"Unknown embedding provider: {settings.embedding_provider}")
    return _embed_client


def embed_text(text_input: str) -> list[float]:
    """Generate embedding for a text string. Results are cached in-memory."""
    if text_input in _embedding_cache:
        return _embedding_cache[text_input]
    result = embed_texts([text_input])[0]
    # Cap cache at 200 entries to avoid unbounded memory growth
    if len(_embedding_cache) < 200:
        _embedding_cache[text_input] = result
    return result


def _build_token_batches(texts: list[str], max_items: int, max_tokens: int) -> list[list[str]]:
    """Split texts into batches that respect both item count and token budget."""
    batches = []
    current_batch = []
    current_tokens = 0
    for t in texts:
        # Rough estimate: 1 token ≈ 4 chars
        est_tokens = len(t) // 4 + 1
        if current_batch and (len(current_batch) >= max_items or current_tokens + est_tokens > max_tokens):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
        current_batch.append(t)
        current_tokens += est_tokens
    if current_batch:
        batches.append(current_batch)
    return batches


def embed_texts(texts: list[str], batch_size: int = 100, max_tokens_per_batch: int = 250_000) -> list[list[float]]:
    """Generate embeddings for multiple texts in batches.

    Uses both item count and token budget to stay under API limits.
    """
    provider = settings.embedding_provider
    model = settings.embedding_model
    all_embeddings = []

    if provider == "openai":
        client = _get_embed_client()
        # Build token-aware batches to stay under OpenAI's 300k token limit
        batches = _build_token_batches(texts, batch_size, max_tokens_per_batch)
        for batch in batches:
            response = client.embeddings.create(model=model, input=batch)
            all_embeddings.extend([d.embedding for d in response.data])

    elif provider == "google":
        genai = _get_embed_client()
        batches = _build_token_batches(texts, batch_size, max_tokens_per_batch)
        for batch in batches:
            result = genai.embed_content(model=model, content=batch)
            all_embeddings.extend(result["embedding"])

    else:
        raise ValueError(f"Unknown embedding provider: {provider}")

    return all_embeddings


def search_chunks(query: str, vehicle_neo4j_id: str | None = None, limit: int = 5) -> list[dict]:
    """Search manual chunks by semantic similarity. Returns list of matching chunks."""
    query_embedding = embed_text(query)
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    engine = _get_engine()

    sql = """
        SELECT id, chunk_text, chunk_type, page_number, neo4j_node_id, source_file,
               embedding <=> CAST(:embedding AS vector) AS distance
        FROM manual_chunks
        WHERE embedding IS NOT NULL
    """
    params = {"embedding": embedding_str, "limit": limit}

    if vehicle_neo4j_id:
        sql += " AND vehicle_neo4j_id = :vehicle_id"
        params["vehicle_id"] = vehicle_neo4j_id

    sql += " ORDER BY embedding <=> CAST(:embedding AS vector) LIMIT :limit"

    with engine.connect() as conn:
        result = conn.execute(text(sql), params)
        rows = result.fetchall()

    return [
        {
            "id": str(row[0]),
            "chunk_text": row[1],
            "chunk_type": row[2],
            "page_number": row[3],
            "neo4j_node_id": row[4],
            "source_file": row[5],
            "distance": float(row[6]),
        }
        for row in rows
    ]


def search_chunks_keyword(keyword: str, limit: int = 5) -> list[dict]:
    """Search manual chunks by keyword (ILIKE). Useful for DTC codes that embed poorly.

    Ranks by keyword frequency — chunks with more mentions are more relevant.
    """
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, chunk_text, chunk_type, page_number, neo4j_node_id, source_file,
                   (LENGTH(chunk_text) - LENGTH(REPLACE(UPPER(chunk_text), UPPER(:kw_raw), '')))
                   / LENGTH(:kw_raw) AS mentions
            FROM manual_chunks
            WHERE chunk_text ILIKE :kw
            ORDER BY mentions DESC, page_number
            LIMIT :limit
        """), {"kw": f"%{keyword}%", "kw_raw": keyword, "limit": limit})
        rows = result.fetchall()
    return [
        {
            "id": str(row[0]),
            "chunk_text": row[1],
            "chunk_type": row[2],
            "page_number": row[3],
            "neo4j_node_id": row[4],
            "source_file": row[5],
            "distance": 0.0,
        }
        for row in rows
    ]
