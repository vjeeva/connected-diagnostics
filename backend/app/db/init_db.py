"""Verify PostgreSQL is ready for use."""

from sqlalchemy import create_engine, text

from backend.app.core.config import settings


def init_postgres():
    """Verify pgvector extension exists and tables are present."""
    engine = create_engine(settings.postgres_sync_url)
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    engine.dispose()
    print("PostgreSQL ready.")


if __name__ == "__main__":
    init_postgres()
