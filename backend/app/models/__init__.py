"""SQLAlchemy models for PostgreSQL tables (MVP subset)."""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.postgres import Base


class ManualChunk(Base):
    __tablename__ = "manual_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vehicle_neo4j_id: Mapped[str] = mapped_column(String, nullable=False)
    neo4j_node_id: Mapped[str | None] = mapped_column(String)
    source_file: Mapped[str] = mapped_column(String, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String, nullable=False, default="procedure")
    embedding = mapped_column(Vector(1536))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiagnosticSession(Base):
    __tablename__ = "diagnostic_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vehicle_neo4j_id: Mapped[str] = mapped_column(String, nullable=False)
    starting_problem_neo4j_id: Mapped[str] = mapped_column(String, nullable=False)
    final_solution_neo4j_id: Mapped[str | None] = mapped_column(String)
    phase: Mapped[str] = mapped_column(String, nullable=False, default="diagnosis")
    extracted_dtcs: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionStep(Base):
    __tablename__ = "session_steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    neo4j_node_id: Mapped[str] = mapped_column(String, nullable=False)
    node_type: Mapped[str] = mapped_column(String, nullable=False)
    user_answer: Mapped[str | None] = mapped_column(Text)
    llm_interpretation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionEstimate(Base):
    __tablename__ = "session_estimates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    solution_neo4j_id: Mapped[str] = mapped_column(String, nullable=False)
    labor_rate_used: Mapped[float] = mapped_column(Float, nullable=False)
    estimate_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    total_parts_low: Mapped[float | None] = mapped_column(Float)
    total_parts_high: Mapped[float | None] = mapped_column(Float)
    total_labor_low: Mapped[float | None] = mapped_column(Float)
    total_labor_high: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionMessage(Base):
    __tablename__ = "session_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
