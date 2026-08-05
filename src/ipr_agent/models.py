"""Pydantic models for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Requests ──────────────────────────────────────────────────────────────

class BatchSubmit(BaseModel):
    """POST /api/v1/batches"""
    application_numbers: list[str] = Field(
        default_factory=list,
        description="Application numbers to look up (e.g. '202641089104')",
        max_length=50,
    )
    patent_numbers: list[str] = Field(
        default_factory=list,
        description="Patent numbers to look up (e.g. '236542')",
        max_length=50,
    )


# ── Responses ─────────────────────────────────────────────────────────────

class BatchProgress(BaseModel):
    total: int
    pending: int
    in_progress: int
    done: int
    failed: int
    not_found: int


class ApplicationResult(BaseModel):
    lookup_type: str
    lookup_value: str
    status: str = Field(description="Queue status: pending | in_progress | done | failed | not_found")
    data: dict[str, Any] | None = Field(
        None, description="Scraped fields (only when status=done)"
    )
    raw_fields: dict[str, str] | None = Field(
        None,
        description="Every label/value pair harvested from the portal page, including "
        "ones not mapped to a typed `data` column — the source of truth when a label "
        "variant isn't in parse.py's ALIASES yet (only when status=done).",
    )
    error: str | None = None
    last_scraped_at: datetime | None = None


class BatchResponse(BaseModel):
    batch_id: str
    status: str = Field(description="Overall: submitted | processing | completed")
    created_at: datetime
    progress: BatchProgress
    results: list[ApplicationResult]


class BatchListItem(BaseModel):
    batch_id: str
    status: str
    created_at: datetime
    progress: BatchProgress


class HealthResponse(BaseModel):
    status: str = "ok"
    worker_running: bool = False
    queue_pending: int = 0
