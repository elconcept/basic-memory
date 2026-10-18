"""Resumable adapter to the existing the-ocr MCP service; no client shell needed."""

import asyncio
import base64
import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

from fastmcp import Client
from pydantic import BaseModel, JsonValue

from basic_memory.wiki_ingest.service import IngestService
from basic_memory.wiki_ingest.sources import snapshot, source_path


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
    state, previous = await service.load()
    if state.active is None or state.entry is None:
        raise ValueError("Start a document before extracting it")
    path, entry = state.active, state.entry
    current = await asyncio.to_thread(snapshot, config, path)
    if current.sha256 != entry.source.sha256:
        raise ValueError("Source changed before extraction")
    if current.sidecar is not None:
        # A prior call may have persisted the sidecar before losing its response.
        entry.source = current
        await service.save(state, previous)
        return {"state": "sidecar_available", "next": "start"}
    if not config.ocr_url:
        raise ValueError("Administrator must configure ocr_url for this project")
    token = os.environ.get(config.ocr_token_env, "") if config.ocr_token_env else None
    if config.ocr_token_env and not token:
        raise ValueError("Configured OCR token environment variable is empty")
    async with Client(config.ocr_url, auth=token) as client:
        if entry.ocr_job is None:
            arguments: dict[str, JsonValue] = {"response_mode": "summary", "wait_seconds": 0}
            if config.ocr_root:
                arguments["path"] = str(PurePosixPath(config.ocr_root) / path)
            else:
                file = source_path(config.documents, path)

                def read_upload() -> bytes:
                    with file.open("rb") as stream:
                        return stream.read(config.max_source_bytes + 1)

                raw = await asyncio.to_thread(read_upload)
                if len(raw) > config.max_source_bytes:
                    raise ValueError("OCR source exceeds configured max_source_bytes")
                if hashlib.sha256(raw).hexdigest() != current.sha256:
                    raise ValueError("Source changed before OCR upload")
                arguments.update(filename=file.name, content_base64=base64.b64encode(raw).decode())
            result = await client.call_tool(config.ocr_prefix + "parse_document", arguments)
            job = OcrJob.model_validate(result.data)
            entry.ocr_job = job.job_id
            entry.ocr_source_sha256 = current.sha256
            service.record(state, entry, f"OCR {job.job_id}: {path}")
            previous = await service.save(state, previous)
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
    if await asyncio.to_thread(snapshot, config, path) != current:
        raise ValueError("Source changed during extraction")
    # This is the sole write to the source mount: a new sidecar, never a source
    # replacement or a direct write to Basic Memory's data directory.
    target = Path(config.documents) / (path + ".md")
    await asyncio.to_thread(publish_sidecar, target, text)
    entry.source = await asyncio.to_thread(snapshot, config, path)
    await service.save(state, previous)
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
