"""Resumable adapter to the existing the-ocr MCP service; no client shell needed."""

import asyncio
import os
import tempfile
from pathlib import Path, PurePosixPath

from fastmcp import Client
from pydantic import BaseModel, JsonValue

from basic_memory.wiki_ingest.service import IngestService, event
from basic_memory.wiki_ingest.sources import snapshot


class OcrJob(BaseModel):
    job_id: str
    status: str
    done: bool


class OcrText(BaseModel):
    content: str
    next_offset: int | None
    complete: bool


async def extract(service: IngestService) -> dict[str, JsonValue]:
    config = service.config
    queue, previous = await service.load()
    if queue.active is None:
        raise ValueError("Start a document before extracting it")
    entry = queue.entries[queue.active]
    current = await asyncio.to_thread(snapshot, config, queue.active)
    if current.sha256 != entry.source.sha256:
        raise ValueError("Source changed before extraction")
    if current.sidecar is not None:
        # A prior call may have persisted the sidecar before losing its response.
        entry.source = current
        await service.save(queue, previous)
        return {"state": "sidecar_available", "next": "start"}
    if not config.ocr_url or not config.ocr_root:
        raise ValueError("Administrator must configure ocr_url and ocr_root for this project")
    async with Client(config.ocr_url) as client:
        if entry.ocr_job is None:
            result = await client.call_tool(
                config.ocr_prefix + "parse_document",
                {
                    "path": str(PurePosixPath(config.ocr_root) / queue.active),
                    "response_mode": "summary",
                    "wait_seconds": 0,
                },
            )
            job = OcrJob.model_validate(result.data)
            entry.ocr_job = job.job_id
            entry.ocr_source_sha256 = current.sha256
            queue.journal.append(event(f"OCR {job.job_id}: {queue.active}"))
            previous = await service.save(queue, previous)
        else:
            result = await client.call_tool(
                config.ocr_prefix + "get_job_status",
                {
                    "job_id": entry.ocr_job,
                    "wait_seconds": 0,
                },
            )
            job = OcrJob.model_validate(result.data)
        if not job.done:
            return {"state": "extracting", "job_id": job.job_id, "next": "extract"}
        if job.status != "succeeded":
            raise ValueError(f"OCR job {job.job_id}: {job.status}; source remains active")
        chunks: list[str] = []
        offset = 0
        total_bytes = 0
        while True:
            result = await client.call_tool(
                config.ocr_prefix + "get_document_text",
                {
                    "job_id": job.job_id,
                    "format": "markdown",
                    "offset": offset,
                    "limit": 40000,
                },
            )
            part = OcrText.model_validate(result.data)
            chunks.append(part.content)
            total_bytes += len(part.content.encode())
            if total_bytes > config.max_text_bytes:
                raise ValueError("OCR text exceeds configured max_text_bytes")
            if part.complete:
                break
            if part.next_offset is None or part.next_offset <= offset:
                raise ValueError("OCR pagination did not advance")
            offset = part.next_offset
    text = "".join(chunks)
    if not text.strip():
        raise ValueError("OCR returned empty text; inspect original")
    if await asyncio.to_thread(snapshot, config, queue.active) != current:
        raise ValueError("Source changed during extraction")
    # This is the sole write to the source mount: a new sidecar, never a source
    # replacement or a direct write to Basic Memory's data directory.
    target = Path(config.documents) / (queue.active + ".md")
    await asyncio.to_thread(publish_sidecar, target, text)
    entry.source = await asyncio.to_thread(snapshot, config, queue.active)
    await service.save(queue, previous)
    return {"state": "extracted", "job_id": job.job_id, "next": "start"}


def publish_sidecar(target: Path, text: str) -> None:
    """Publish a complete sidecar atomically without replacing an existing file."""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=target.parent,
            prefix=".ocr-",
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            os.unlink(temporary)
