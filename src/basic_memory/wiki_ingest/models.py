"""Portable, validated ingest state stored as ordinary Basic Memory notes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceConfig(BaseModel):
    """Administrator-owned mounts; callers can never choose a server root."""

    model_config = ConfigDict(extra="forbid")
    documents: str
    legacy_workspace: str | None = None
    priority: list[str] = Field(default_factory=list)
    ocr_url: str | None = None
    ocr_root: str | None = None
    ocr_prefix: str = ""
    max_text_bytes: int = Field(default=20 * 1024 * 1024, gt=0)


class Source(BaseModel):
    path: str
    sha256: str
    sidecar: str | None = None
    sidecar_sha256: str | None = None
    size: int


class Entry(BaseModel):
    source: Source
    state: Literal["queued", "legacy", "active", "done", "skipped", "changed", "missing"] = "queued"
    note: str | None = None
    note_checksum: str | None = None
    next_offset: int = 0
    resume_offset: int = 0
    related_notes: list[str] = Field(default_factory=list)
    reason: str = ""
    ocr_job: str | None = None
    ocr_source_sha256: str | None = None


class Queue(BaseModel):
    version: Literal["1"] = "1"
    entries: dict[str, Entry] = Field(default_factory=dict)
    active: str | None = None
    journal: list[str] = Field(default_factory=list)


class Step(BaseModel):
    title: str
    state: Literal["pending", "active", "done", "failed", "skipped"] = "pending"
    evidence: str = ""


class Plan(BaseModel):
    title: str
    content: str
    steps: list[Step] = Field(min_length=1)
    state: Literal["active", "paused", "done", "abandoned"] = "active"
    journal: list[str] = Field(default_factory=list)
