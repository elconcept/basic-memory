"""A resumable, single-document ingest protocol shared by every MCP client.

The caller supplies interpretation. The server owns source identity, full-text
capture, read coverage, durable checkpoints, and completion checks.
"""

import asyncio
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import PurePosixPath

from pydantic import JsonValue

from basic_memory.utils import generate_permalink
from basic_memory.wiki_ingest.models import Entry, Queue, SourceConfig
from basic_memory.wiki_ingest.sources import inventory, read_text, snapshot
from basic_memory.wiki_ingest.pdf_review import reviewed_text
from basic_memory.wiki_ingest.store import Note, NoteStore

QUEUE_ID = "_ingest/wiki-ingest-state"


def event(message: str) -> str:
    return f"{datetime.now(UTC).isoformat()} {message}"


def raw_body(text: str, digest: str) -> str:
    # An opaque fence prevents source headings/frontmatter/instructions from
    # becoming workflow metadata while retaining every source character.
    fence = "`" * (max((len(x) for x in re.findall(r"`+", text)), default=2) + 1)
    return f"# Source text\n\nSHA-256: {digest}\n\n{fence}text\n{text}\n{fence}\n"


class IngestService:
    def __init__(self, store: NoteStore, config: SourceConfig):
        self.store = store
        self.config = config

    async def load(self) -> tuple[Queue, Note | None]:
        note = await self.store.read(QUEUE_ID)
        return (Queue.model_validate(note.metadata["wiki_ingest"]) if note else Queue(), note)

    async def save(self, queue: Queue, previous: Note | None) -> Note:
        counts = Counter(e.state for e in queue.entries.values())
        content = "# Wiki ingest\n\n" + json.dumps(dict(counts), ensure_ascii=False)
        content += "\n\n## Journal\n\n" + "\n".join(f"- {line}" for line in queue.journal)
        return await self.store.write(
            "wiki-ingest-state",
            "_ingest",
            content,
            {"type": "wiki_ingest", "wiki_ingest": queue.model_dump(mode="json")},
            previous,
        )

    async def status(self, offset: int, limit: int) -> dict[str, JsonValue]:
        queue, _ = await self.load()
        keys = sorted(queue.entries)
        selected = keys[offset : offset + limit]
        return {
            "active": queue.active,
            "counts": dict(Counter(e.state for e in queue.entries.values())),
            "total": len(keys),
            "next_offset": offset + len(selected),
            "complete": offset + len(selected) >= len(keys),
            "entries": {k: queue.entries[k].model_dump(mode="json") for k in selected},
        }

    async def refresh(self) -> dict[str, JsonValue]:
        queue, previous = await self.load()
        sources = await asyncio.to_thread(inventory, self.config)
        for path, source in sources.items():
            old = queue.entries.get(path)
            if old is None:
                queue.entries[path] = Entry(source=source)
            elif old.source != source or old.state == "missing":
                if queue.active == path:
                    raise ValueError(
                        f"Active source changed: {path}. Skip with a reason before refresh."
                    )
                queue.entries[path] = Entry(
                    source=source, state="changed", note=old.note, reason=old.reason
                )
        for path, entry in queue.entries.items():
            if path not in sources:
                if queue.active == path:
                    raise ValueError(f"Active source missing: {path}")
                entry.state = "missing"
        queue.journal.append(event(f"Inventory: {len(sources)} sources"))
        await self.save(queue, previous)
        return {
            "sources": len(sources),
            "active": queue.active,
            "counts": dict(Counter(e.state for e in queue.entries.values())),
        }

    async def start(self, path: str | None) -> dict[str, JsonValue]:
        queue, previous = await self.load()
        if queue.active:
            if path and path != queue.active:
                raise ValueError(f"Finish the active document first: {queue.active}")
            path = queue.active
        if path is None:
            candidates = [
                p for p, e in queue.entries.items() if e.state in {"queued", "changed", "legacy"}
            ]
            priority = {p: i for i, p in enumerate(self.config.priority)}
            candidates.sort(
                key=lambda p: (
                    queue.entries[p].state != "changed",
                    priority.get(p.split("/")[0], len(priority)),
                    p,
                )
            )
            if not candidates:
                return {"state": "empty", "next": "refresh to discover new sources"}
            path = candidates[0]
        if path not in queue.entries:
            raise ValueError("Source is not in the inventory; refresh first")
        entry = queue.entries[path]
        if entry.state not in {"queued", "changed", "legacy", "active"}:
            raise ValueError(f"Cannot start source in state {entry.state}")
        current = await asyncio.to_thread(snapshot, self.config, path)
        if current != entry.source:
            raise ValueError("Source changed since inventory; refresh first")
        if queue.active is None:
            queue.active = path
            entry.state = "active"
            entry.note = None
            entry.note_checksum = None
            queue.journal.append(event(f"Started: {path}"))
            previous = await self.save(queue, previous)
        if entry.source.sidecar is None:
            return {"state": "needs_extraction", "path": path, "next": "extract"}
        if PurePosixPath(path).suffix.lower() == ".pdf":
            text = await reviewed_text(self.store, entry.source)
            if text is None:
                # Older active queues may already point at an unverified raw sidecar.
                # Preserve that historical note but require a new reviewed snapshot.
                entry.note = None
                entry.next_offset = entry.resume_offset = 0
                await self.save(queue, previous)
                return {
                    "state": "needs_pdf_review",
                    "path": path,
                    "next": "read candidate sidecar; inspect_page and verify_page for every PDF page; start",
                }
        else:
            text = await asyncio.to_thread(read_text, self.config, entry.source)
        if len(text.encode()) > self.config.max_text_bytes:
            raise ValueError("Transcription exceeds configured max_text_bytes")
        digest = hashlib.sha256(text.encode()).hexdigest()
        identity = hashlib.sha256(
            (path + "\0" + entry.source.sha256 + "\0" + digest).encode()
        ).hexdigest()
        title = "source-" + identity
        directory = str(PurePosixPath("dokumenty") / PurePosixPath(path).parent)
        identifier = generate_permalink(directory + "/" + title)
        body = raw_body(text, digest)
        note = await self.store.read(identifier)
        if note is None:
            note = await self.store.write(
                title,
                directory,
                body,
                {
                    "type": "dokument",
                    "source_path": path,
                    "source_sha256": entry.source.sha256,
                    "text_sha256": digest,
                    "sidecar": entry.source.sidecar,
                    "stan_ingestu": "pdf_reviewed" if path.lower().endswith(".pdf") else "raw",
                },
            )
        # Persisted content may contain API-owned frontmatter. The exact source
        # block must still be present; a guessed title or checksum is insufficient.
        if body.rstrip() not in note.content:
            raise ValueError("Source note read-back differs from the full source text")
        if await asyncio.to_thread(snapshot, self.config, path) != entry.source:
            raise ValueError("Source changed while its note was being accepted")
        entry.note = note.identifier
        entry.note_checksum = note.checksum
        await self.save(queue, previous)
        return {
            "state": "active",
            "path": path,
            "note": note.identifier,
            "characters": len(text),
            "next_offset": entry.next_offset,
            "resume_offset": entry.resume_offset,
            "next": "read all text, write findings with source links, then finish",
        }

    async def read(self, offset: int, limit: int) -> dict[str, JsonValue]:
        queue, previous = await self.load()
        if queue.active is None:
            raise ValueError("No active source; start first")
        entry = queue.entries[queue.active]
        if await asyncio.to_thread(snapshot, self.config, queue.active) != entry.source:
            raise ValueError("Source changed during ingest")
        text = (
            await self.accepted_text(entry)
            if entry.note
            else await asyncio.to_thread(read_text, self.config, entry.source)
        )
        if entry.note is None:
            return {
                "state": "unverified_sidecar",
                "text": text[offset : offset + limit],
                "next_offset": min(offset + limit, len(text)),
                "complete": offset + limit >= len(text),
                "next": "Compare candidate with original PDF images; do not publish as source",
            }
        if offset > entry.next_offset or offset > len(text):
            raise ValueError(f"Read gap: resume at offset {entry.next_offset}")
        end = min(offset + limit, len(text))
        entry.resume_offset = offset
        entry.next_offset = max(entry.next_offset, end)
        await self.save(queue, previous)
        return {
            "path": queue.active,
            "note": entry.note,
            "text": text[offset:end],
            "offset": offset,
            "next_offset": end,
            "total": len(text),
            "complete": end == len(text),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }

    async def accepted_text(self, entry: Entry) -> str:
        if entry.source.path.lower().endswith(".pdf"):
            text = await reviewed_text(self.store, entry.source)
            if text is None:
                raise ValueError("All PDF pages require visual review before publication")
            if len(text.encode()) > self.config.max_text_bytes:
                raise ValueError("Transcription exceeds configured max_text_bytes")
            return text
        return await asyncio.to_thread(read_text, self.config, entry.source)

    async def finish(self, related: list[str], reason: str) -> dict[str, JsonValue]:
        queue, previous = await self.load()
        if queue.active is None:
            raise ValueError("No active source")
        entry = queue.entries[queue.active]
        if await asyncio.to_thread(snapshot, self.config, queue.active) != entry.source:
            raise ValueError("Source changed; completion refused")
        text = await self.accepted_text(entry)
        if entry.next_offset != len(text) or not entry.note:
            raise ValueError("Full text has not been read; continue from next_offset")
        source_note = await self.store.read(entry.note)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if source_note is None or raw_body(text, digest).rstrip() not in source_note.content:
            raise ValueError("Full source text is missing or changed in Basic Memory")
        if not related and not reason.strip():
            raise ValueError("Supply related findings notes, or explain why this source adds none")
        verified = []
        for identifier in dict.fromkeys(related):
            note = await self.store.read(identifier)
            if note is None or note.identifier == entry.note:
                raise ValueError(f"Missing/distinct findings note required: {identifier}")
            if f"[[{entry.note}]]" not in note.content and f"[[{entry.note}|" not in note.content:
                raise ValueError(f"Findings note must link to [[{entry.note}]]: {identifier}")
            verified.append(note.identifier)
        entry.related_notes = verified
        entry.reason = reason
        entry.state = "done"
        queue.journal.append(event(f"Completed: {queue.active}; findings={verified}; {reason}"))
        queue.active = None
        await self.save(queue, previous)
        return {"state": "done", "note": entry.note, "related_notes": verified}

    async def skip(self, reason: str) -> dict[str, JsonValue]:
        if not reason.strip():
            raise ValueError("An explicit reason is required")
        queue, previous = await self.load()
        if queue.active is None:
            raise ValueError("No active source")
        path = queue.active
        queue.entries[path].state = "skipped"
        queue.entries[path].reason = reason
        queue.active = None
        queue.journal.append(event(f"Skipped: {path}; {reason}"))
        await self.save(queue, previous)
        return {"state": "skipped", "path": path, "reason": reason}

    async def index(self) -> dict[str, JsonValue]:
        queue, _ = await self.load()
        content = "# Ingest source index\n\n"
        for path, entry in sorted(queue.entries.items()):
            content += f"- [{entry.state}] {path}"
            if entry.note:
                content += f" — [[{entry.note}]]"
            content += "".join(f" — [[{note}]]" for note in entry.related_notes) + "\n"
        old = await self.store.read("_ingest/wiki-ingest-index")
        note = await self.store.write(
            "wiki-ingest-index", "_ingest", content, {"type": "wiki_ingest_index"}, old
        )
        return {"note": note.identifier, "sources": len(queue.entries)}
