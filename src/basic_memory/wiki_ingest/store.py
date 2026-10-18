"""All state and document writes use the existing authorized knowledge API."""

from dataclasses import dataclass
from typing import Protocol

from fastmcp.exceptions import ToolError
from httpx import HTTPStatusError
from pydantic import JsonValue

from basic_memory.mcp.clients.knowledge import KnowledgeClient
from basic_memory.schemas.base import Entity
from basic_memory.schemas.v2.note_write import NoteCreated, NoteUpdated


@dataclass(frozen=True)
class Note:
    identifier: str
    checksum: str
    content: str
    metadata: dict[str, JsonValue]


class NoteStore(Protocol):
    async def read(self, identifier: str) -> Note | None: ...

    async def write(
        self,
        title: str,
        directory: str,
        content: str,
        metadata: dict[str, JsonValue],
        expected: Note | None = None,
    ) -> Note: ...


class ApiNoteStore:
    def __init__(self, client: KnowledgeClient):
        self.client = client

    async def read(self, identifier: str) -> Note | None:
        try:
            resolved = await self.client.resolve_entity_response(identifier, strict=True)
        except ToolError as error:
            cause = error.__cause__
            if isinstance(cause, HTTPStatusError) and cause.response.status_code == 404:
                return None
            raise
        entity = await self.client.get_entity(resolved.external_id)
        if entity.content is None or not entity.db_checksum or not entity.permalink:
            raise ValueError(f"{identifier}: accepted Markdown/checksum unavailable")
        return Note(
            entity.permalink, entity.db_checksum, entity.content, entity.entity_metadata or {}
        )

    async def write(
        self,
        title: str,
        directory: str,
        content: str,
        metadata: dict[str, JsonValue],
        expected: Note | None = None,
    ) -> Note:
        outcome = await self.client.write_note(
            Entity(
                title=title,
                directory=directory,
                content=content,
                note_type=str(metadata.get("type", "note")),
                entity_metadata=metadata,
            ),
            overwrite=expected is not None,
            expected_checksum=expected.checksum if expected else None,
        )
        if not isinstance(outcome, (NoteCreated, NoteUpdated)):
            raise ValueError(f"Write refused: {outcome.model_dump_json()}. Read before retrying.")
        entity = outcome.entity
        reread = await self.read(entity.external_id)
        if reread is None or reread.checksum != entity.db_checksum:
            raise ValueError("Read-back did not confirm the accepted note revision")
        return reread
