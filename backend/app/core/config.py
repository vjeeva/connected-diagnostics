from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    postgres_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/diagnostics"
    extraction_provider: str = "anthropic"
    extraction_model: str = "claude-haiku-4-5-20251001"
    chat_provider: str = "anthropic"
    chat_model: str = "claude-sonnet-4-20250514"  # strong model: open-ended, work orders, no-graph-match
    chat_model_light: str = ""                   # cheap model: graph traversal, known procedures (defaults to chat_model)
    interpret_provider: str = ""                  # defaults to chat_provider if empty
    interpret_model: str = ""                     # defaults to chat_model_light if empty
    vision_provider: str = "anthropic"
    vision_model: str = "claude-haiku-4-5-20251001"
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    ingestion_workers: int = 8
    chunk_max_chars: int = 16000
    chunk_overlap_pages: int = 2
    trust_mode: str = "bootstrap"  # bootstrap, hybrid, reputation

    @property
    def light_model(self) -> str:
        """Resolve the light/cheap chat model, falling back to the strong model."""
        return self.chat_model_light or self.chat_model

    # Sync version for non-async contexts (ingestion CLI)
    @property
    def postgres_sync_url(self) -> str:
        return self.postgres_url.replace("postgresql+asyncpg", "postgresql+psycopg2")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
