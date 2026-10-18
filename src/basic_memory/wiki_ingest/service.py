"""A resumable, single-document ingest protocol shared by every MCP client.

The caller supplies interpretation. The server owns source identity, full-text
capture, read coverage, durable checkpoints, and completion checks.

State is split by write frequency. The small state note holds the active entry
and its read offsets, so read/extract/review touch only that note. The inventory
lives in fixed hash shards, written when an entry changes state or on refresh.
A corpus of thousands of sources therefore never rewrites one large note.
"""

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import PurePosixPath

from pydantic import JsonValue

from basic_memory.utils import generate_permalink
from basic_memory.wiki_ingest.models import Entry, Queue, SourceConfig, State
from basic_memory.wiki_ingest.sources import listing, read_text, snapshot
from basic_memory.wiki_ingest.pdf_review import reviewed_text
from basic_memory.wiki_ingest.store import Note, NoteStore

STATE_ID = "_ingest/wiki-ingest-state"
SHARDS = 64
JOURNAL_LIMIT = 200
# Budget for starting further shards in one refresh call. Creating a large shard
# note (embedding) can take a minute, so calls stay well inside client timeouts;
# later calls continue with the pending shards.
REFRESH_SECONDS = 20.0


def event(message: str) -> str:
    return f"{datetime.now(UTC).isoformat()} {message}"


def raw_body(text: str, digest: str) -> str:
    # An opaque fence prevents source headings/frontmatter/instructions from
    # becoming workflow metadata while retaining every source character.
    fence = "`" * (max((len(x) for x in re.findall(r"`+", text)), default=2) + 1)
    return f"# Source text\n\nSHA-256: {digest}\n\n{fence}text\n{text}\n{fence}\n"


def shard_of(path: str) -> int:
    return int(hashlib.sha256(path.encode()).hexdigest()[:8], 16) % SHARDS


def shard_id(index: int) -> str:
    return f"_ingest/inventory/shard-{index:02d}"


Shards = dict[int, tuple[dict[str, Entry], Note | None]]


class IngestService:
    def __init__(self, store: NoteStore, config: SourceConfig):
        self.store = store
        self.config = config

    # -- persistence -----------------------------------------------------

    async def load(self) -> tuple[State, Note | None]:
        note = await self.store.read(STATE_ID)
        if note is None:
            return State(), None
        data = note.metadata["wiki_ingest"]
        if isinstance(data, dict) and data.get("version", "1") == "1":
            return await self.upgrade(Queue.model_validate(data), note)
        return State.model_validate(data), note

    async def upgrade(self, queue: Queue, note: Note) -> tuple[State, Note]:
        """Split a version 1 queue into shards; keep its full journal verbatim."""
        if queue.journal and await self.store.read("_ingest/wiki-ingest-journal-v1") is None:
            await self.store.write(
                "wiki-ingest-journal-v1",
                "_ingest",
                "# Wiki ingest journal (v1)\n\n" + "\n".join(f"- {x}" for x in queue.journal),
                {"type": "wiki_ingest_journal"},
            )
        grouped: dict[int, dict[str, Entry]] = {}
        for path, entry in queue.entries.items():
            grouped.setdefault(shard_of(path), {})[path] = entry
        for index, entries in grouped.items():
            current, previous = await self.load_shard(index)
            await self.save_shard(index, current | entries, previous)
        state = State(
            active=queue.active,
            entry=queue.entries.get(queue.active) if queue.active else None,
            journal=queue.journal[-JOURNAL_LIMIT:],
        )
        self.record(state, None, f"Upgraded state: {len(queue.entries)} sources in shards")
        return state, await self.save(state, note)

    async def save(self, state: State, previous: Note | None) -> Note:
        state.journal = state.journal[-JOURNAL_LIMIT:]
        content = f"# Wiki ingest\n\nActive: {state.active or '-'}\n"
        content += "\n## Journal\n\n" + "\n".join(f"- {line}" for line in state.journal)
        return await self.store.write(
            "wiki-ingest-state",
            "_ingest",
            content,
            {"type": "wiki_ingest", "wiki_ingest": state.model_dump(mode="json")},
            previous,
        )

    async def load_shard(self, index: int) -> tuple[dict[str, Entry], Note | None]:
        note = await self.store.read(shard_id(index))
        if note is None:
            return {}, None
        raw = note.metadata["wiki_ingest_shard"]
        assert isinstance(raw, dict)
        return {path: Entry.model_validate(value) for path, value in raw.items()}, note

    async def save_shard(
        self, index: int, entries: dict[str, Entry], previous: Note | None
    ) -> Note:
        ordered = dict(sorted(entries.items()))
        content = f"# Wiki ingest inventory {index:02d}\n\n"
        content += "\n".join(f"- [{e.state}] {path}" for path, e in ordered.items())
        return await self.store.write(
            f"shard-{index:02d}",
            "_ingest/inventory",
            content,
            {
                "type": "wiki_ingest_shard",
                "wiki_ingest_shard": {p: e.model_dump(mode="json") for p, e in ordered.items()},
            },
            previous,
        )

    async def load_shards(self) -> Shards:
        loaded = await asyncio.gather(*(self.load_shard(i) for i in range(SHARDS)))
        return dict(enumerate(loaded))

    async def put(self, path: str, entry: Entry) -> None:
        index = shard_of(path)
        entries, previous = await self.load_shard(index)
        entries[path] = entry
        await self.save_shard(index, entries, previous)

    async def entries(self, state: State) -> dict[str, Entry]:
        merged = {
            p: e for entries, _ in (await self.load_shards()).values() for p, e in entries.items()
        }
        if state.active and state.entry:
            merged[state.active] = state.entry
        return merged

    @staticmethod
    def record(state: State, entry: Entry | None, message: str) -> None:
        line = event(message)
        state.journal.append(line)
        if entry is not None:
            entry.history.append(line)

    def active(self, state: State) -> tuple[str, Entry]:
        if state.active is None or state.entry is None:
            raise ValueError("No active source; start first")
        return state.active, state.entry

    # -- protocol --------------------------------------------------------

    async def status(self, offset: int, limit: int) -> dict[str, JsonValue]:
        state, _ = await self.load()
        entries = await self.entries(state)
        keys = sorted(entries)
        selected = keys[offset : offset + limit]
        return {
            "active": state.active,
            "counts": dict(Counter(e.state for e in entries.values())),
            "total": len(keys),
            "next_offset": offset + len(selected),
            "complete": offset + len(selected) >= len(keys),
            "entries": {k: entries[k].model_dump(mode="json") for k in selected},
        }

    async def refresh(self) -> dict[str, JsonValue]:
        """Reconcile shards one at a time until the time budget is spent.

        Each shard is reconciled completely (hash new/touched files, mark
        missing ones) and written before the next starts, so an interrupted
        call loses no finished work and a later call resumes with the rest.
        """
        state, previous = await self.load()
        deadline = time.monotonic() + REFRESH_SECONDS
        files = await asyncio.to_thread(listing, self.config)
        grouped: dict[int, dict[str, str]] = {}
        for path, current in files.items():
            grouped.setdefault(shard_of(path), {})[path] = current
        if not state.refresh_pending:
            state.refresh_pending = list(range(SHARDS))
        done = 0
        while state.refresh_pending and (done == 0 or time.monotonic() < deadline):
            index = state.refresh_pending[0]
            entries, note = await self.load_shard(index)
            if await asyncio.to_thread(self.reconcile, state, entries, grouped.get(index, {})):
                await self.save_shard(index, entries, note)
            state.refresh_pending.pop(0)
            done += 1
        complete = not state.refresh_pending
        self.record(
            state,
            None,
            f"Inventory: {len(files)} sources; {done} shards"
            + ("" if complete else f"; {len(state.refresh_pending)} pending"),
        )
        await self.save(state, previous)
        result: dict[str, JsonValue] = {"sources": len(files), "complete": complete}
        if complete:
            entries = await self.entries(state)
            result["active"] = state.active
            result["counts"] = dict(Counter(e.state for e in entries.values()))
        else:
            result["pending_shards"] = len(state.refresh_pending)
            result["next"] = "refresh again; reconciliation continues with pending shards"
        return result

    def reconcile(self, state: State, entries: dict[str, Entry], files: dict[str, str]) -> bool:
        """Bring one shard in line with the mount; return whether it changed."""
        changed = False
        for path, current in files.items():
            old = state.entry if path == state.active else entries.get(path)
            if old is not None and old.stamp == current and old.state != "missing":
                continue
            source = snapshot(self.config, path)
            if old is None:
                entries[path] = Entry(source=source, stamp=current)
            elif old.source != source or old.state == "missing":
                if state.active == path:
                    raise ValueError(
                        f"Active source changed: {path}. Skip with a reason before refresh."
                    )
                entries[path] = Entry(
                    source=source,
                    stamp=current,
                    state="changed",
                    note=old.note,
                    reason=old.reason,
                    history=old.history,
                )
            else:
                entries[path].stamp = current
            changed = True
        for path, entry in entries.items():
            if path not in files and entry.state != "missing":
                if state.active == path:
                    raise ValueError(f"Active source missing: {path}")
                entry.state = "missing"
                changed = True
        return changed

    async def start(self, path: str | None) -> dict[str, JsonValue]:
        state, previous = await self.load()
        if state.active:
            if path and path != state.active:
                raise ValueError(f"Finish the active document first: {state.active}")
            path, entry = self.active(state)
        else:
            if path is None:
                entries = await self.entries(state)
                candidates = [
                    p for p, e in entries.items() if e.state in {"queued", "changed", "legacy"}
                ]
                priority = {p: i for i, p in enumerate(self.config.priority)}
                candidates.sort(
                    key=lambda p: (
                        entries[p].state != "changed",
                        priority.get(p.split("/")[0], len(priority)),
                        p,
                    )
                )
                if not candidates:
                    return {"state": "empty", "next": "refresh to discover new sources"}
                path = candidates[0]
            shard, _ = await self.load_shard(shard_of(path))
            if path not in shard:
                raise ValueError("Source is not in the inventory; refresh first")
            entry = shard[path]
            if entry.state not in {"queued", "changed", "legacy"}:
                raise ValueError(f"Cannot start source in state {entry.state}")
        current = await asyncio.to_thread(snapshot, self.config, path)
        if current != entry.source:
            raise ValueError("Source changed since inventory; refresh first")
        if state.active is None:
            entry.state = "active"
            entry.note = None
            entry.note_checksum = None
            entry.next_offset = entry.resume_offset = 0
            self.record(state, entry, f"Started: {path}")
            state.active, state.entry = path, entry
            # The state note is the claim: a concurrent start fails on its checksum.
            previous = await self.save(state, previous)
            await self.put(path, entry)
        if entry.source.sidecar is None:
            return {"state": "needs_extraction", "path": path, "next": "extract"}
        if PurePosixPath(path).suffix.lower() == ".pdf":
            text = await reviewed_text(self.store, entry.source)
            if text is None:
                # Older active queues may already point at an unverified raw sidecar.
                # Preserve that historical note but require a new reviewed snapshot.
                entry.note = None
                entry.next_offset = entry.resume_offset = 0
                await self.save(state, previous)
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
        await self.save(state, previous)
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
        state, previous = await self.load()
        path, entry = self.active(state)
        if await asyncio.to_thread(snapshot, self.config, path) != entry.source:
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
        await self.save(state, previous)
        return {
            "path": path,
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

    async def close(self, state: State, previous: Note | None, path: str, entry: Entry) -> None:
        # Shard first: if the state write then fails, repeating the call is idempotent.
        await self.put(path, entry)
        state.active, state.entry = None, None
        await self.save(state, previous)

    async def finish(self, related: list[str], reason: str) -> dict[str, JsonValue]:
        state, previous = await self.load()
        if state.active is None:
            raise ValueError("No active source")
        path, entry = self.active(state)
        if await asyncio.to_thread(snapshot, self.config, path) != entry.source:
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
        self.record(state, entry, f"Completed: {path}; findings={verified}; {reason}")
        await self.close(state, previous, path, entry)
        return {"state": "done", "note": entry.note, "related_notes": verified}

    async def skip(self, reason: str) -> dict[str, JsonValue]:
        if not reason.strip():
            raise ValueError("An explicit reason is required")
        state, previous = await self.load()
        if state.active is None:
            raise ValueError("No active source")
        path, entry = self.active(state)
        entry.state = "skipped"
        entry.reason = reason
        self.record(state, entry, f"Skipped: {path}; {reason}")
        await self.close(state, previous, path, entry)
        return {"state": "skipped", "path": path, "reason": reason}

    async def index(self) -> dict[str, JsonValue]:
        state, _ = await self.load()
        entries = await self.entries(state)
        content = "# Ingest source index\n\n"
        content += json.dumps(dict(Counter(e.state for e in entries.values())), ensure_ascii=False)
        content += "\n\n"
        for path, entry in sorted(entries.items()):
            content += f"- [{entry.state}] {path}"
            if entry.note:
                content += f" — [[{entry.note}]]"
            content += "".join(f" — [[{note}]]" for note in entry.related_notes) + "\n"
        old = await self.store.read("_ingest/wiki-ingest-index")
        note = await self.store.write(
            "wiki-ingest-index", "_ingest", content, {"type": "wiki_ingest_index"}, old
        )
        return {"note": note.identifier, "sources": len(entries)}
