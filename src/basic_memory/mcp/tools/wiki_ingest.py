"""Server-side wiki ingestion, available identically to browser and desktop clients."""

import os
import base64
import json
from mcp.types import ImageContent, TextContent
from typing import Annotated, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field, JsonValue, TypeAdapter

from basic_memory.mcp.clients.knowledge import KnowledgeClient
from basic_memory.mcp.project_context import get_project_client
from basic_memory.mcp.server import mcp
from basic_memory.wiki_ingest.migration import migrate
from basic_memory.wiki_ingest.models import SourceConfig
from basic_memory.wiki_ingest.ocr import extract
from basic_memory.wiki_ingest.quality import coverage
from basic_memory.wiki_ingest.service import IngestService
from basic_memory.wiki_ingest.pdf_review import inspect_page, verify_page
from basic_memory.wiki_ingest.store import ApiNoteStore


@mcp.tool(
    title="Ingest sources into wiki",
    description=(
        "Server-side resumable ingest. refresh inventories configured source mounts; "
        "start requires visual review for PDFs; inspect_page returns an original page image; "
        "verify_page accepts corrected transcription with a receipt; extract runs OCR if missing; read returns "
        "successive text chunks; finish verifies source, complete reading and linked "
        "findings notes. status resumes across clients; index builds navigation; "
        "migrate preserves one legacy file verbatim. No client shell required. "
        "Read man('wiki-ingest') for the complete workflow."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
async def wiki_ingest(
    action: Literal[
        "status",
        "refresh",
        "start",
        "extract",
        "read",
        "finish",
        "skip",
        "index",
        "migrate",
        "audit",
        "inspect_page",
        "verify_page",
    ],
    project: str,
    path: str | None = None,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=40000)] = 12000,
    related_notes: list[str] | None = None,
    reason: str = "",
    page: Annotated[int, Field(ge=1)] = 1,
    receipt: str = "",
    markdown: str = "",
    project_id: str | None = None,
    context: Context | None = None,
) -> dict[str, JsonValue] | list[TextContent | ImageContent]:
    """Paths are relative to an administrator-configured source mount.

    Configuration keys are project UUIDs (not names), so equal names in separate
    hosted workspaces cannot grant access to another project's documents.
    offset/limit count characters for read, entries for status/migrate.
    """
    async with get_project_client(project, context=context, project_id=project_id) as (
        client,
        active,
    ):
        configs = TypeAdapter(dict[str, SourceConfig]).validate_json(
            os.environ.get("BASIC_MEMORY_INGEST_SOURCES", "{}")
        )
        config = configs.get(active.external_id)
        if config is None:
            raise ToolError("No ingest source mount configured for this project UUID")
        service = IngestService(ApiNoteStore(KnowledgeClient(client, active.external_id)), config)
        try:
            match action:
                case "status":
                    return await service.status(offset, min(limit, 100))
                case "refresh":
                    return await service.refresh()
                case "start":
                    return await service.start(path)
                case "extract":
                    return await extract(service)
                case "inspect_page" | "verify_page":
                    queue, _ = await service.load()
                    if queue.active is None:
                        raise ValueError("Start a PDF before reviewing it")
                    entry = queue.entries[queue.active]
                    if entry.note is not None:
                        raise ValueError(
                            "Source already captured; skip and restart to revise review"
                        )
                    if action == "inspect_page":
                        image, info = await inspect_page(service.store, config, entry.source, page)
                        return [
                            TextContent(type="text", text=json.dumps(info)),
                            ImageContent(
                                type="image",
                                mime_type="image/png",
                                data=base64.b64encode(image).decode(),
                            ),
                        ]
                    return await verify_page(
                        service.store, config, entry.source, page, receipt, markdown, reason
                    )
                case "read":
                    return await service.read(offset, limit)
                case "finish":
                    return await service.finish(related_notes or [], reason)
                case "skip":
                    return await service.skip(reason)
                case "index":
                    return await service.index()
                case "migrate":
                    return await migrate(service, path, offset, min(limit, 100))
                case "audit":
                    queue, _ = await service.load()
                    if queue.active is None:
                        raise ValueError("Start a source before auditing findings")
                    source = await service.accepted_text(queue.entries[queue.active])
                    findings = []
                    for identifier in related_notes or []:
                        note = await service.store.read(identifier)
                        if note is None:
                            raise ValueError(f"Missing findings note: {identifier}")
                        findings.append(note.content)
                    return coverage(source, "\n".join(findings))
        except (ValueError, OSError) as error:
            raise ToolError(str(error)) from error
