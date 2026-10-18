"""Read-only import of legacy snapshots and registries, one file per call."""

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

from pydantic import JsonValue

from basic_memory.wiki_ingest.models import Entry
from basic_memory.wiki_ingest.service import IngestService, event, raw_body
from basic_memory.wiki_ingest.sources import EXCLUDED, source_path

LEGACY_ROOTS = {"00_basic_memory", "00_wiki", "_ingest", "_plany", "_narzedzia"}


async def migrate(
    service: IngestService, path: str | None, offset: int, limit: int
) -> dict[str, JsonValue]:
    root = service.config.legacy_workspace
    if root is None:
        raise ValueError("No legacy_workspace configured by the administrator")
    if path is None:
        paths = []
        for folder in sorted(LEGACY_ROOTS):
            base = Path(root) / folder
            if not base.exists():
                continue
            for directory, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [
                    name
                    for name in dirs
                    if name not in EXCLUDED
                    and name not in {"git", "worktree"}
                    and not name.startswith(".")
                    and not (Path(directory) / name).is_symlink()
                ]
                for name in files:
                    item = Path(directory) / name
                    if (
                        not name.startswith(".")
                        and not item.is_symlink()
                        and item.suffix in {".md", ".json", ".jsonl"}
                    ):
                        paths.append(item.relative_to(root).as_posix())
        paths.sort()
        selected = paths[offset : offset + limit]
        return {
            "paths": selected,
            "total": len(paths),
            "next_offset": offset + len(selected),
            "complete": offset + len(selected) >= len(paths),
        }
    if not PurePosixPath(path).parts or PurePosixPath(path).parts[0] not in LEGACY_ROOTS:
        raise ValueError("Only legacy wiki, memory, ingest and plan snapshots can be imported")
    file = source_path(root, path)
    if file.suffix not in {".md", ".json", ".jsonl"}:
        raise ValueError("Only Markdown and JSON legacy snapshots can be imported")

    def bounded_read() -> bytes:
        with file.open("rb") as stream:
            return stream.read(service.config.max_text_bytes + 1)

    raw = await asyncio.to_thread(bounded_read)
    if len(raw) > service.config.max_text_bytes:
        raise ValueError("Legacy file exceeds max_text_bytes")
    text = raw.decode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    title = "legacy-" + hashlib.sha256((path + digest).encode()).hexdigest()
    identifier = "_ingest/legacy/" + title
    body = raw_body(text, digest)
    note = await service.store.read(identifier)
    if note is None:
        note = await service.store.write(
            title,
            "_ingest/legacy",
            body,
            {"type": "legacy_snapshot", "source_path": path, "source_sha256": digest},
        )
    if body.rstrip() not in note.content:
        raise ValueError("Legacy snapshot differs on read-back")
    # Keep all historical fields in the verbatim snapshot. A previous 'done'
    # flag is not evidence that today's source and findings were verified.
    if path == "_ingest/rejestr_bm.json":
        queue, previous = await service.load()
        if queue.active:
            raise ValueError("Finish active document before importing legacy registry")
        legacy = json.loads(text)
        for old_path, old in legacy["dokumenty"].items():
            relative = old_path.removeprefix("Dokumentacja/")
            if relative not in queue.entries:
                continue
            entry = queue.entries[relative]
            if entry.state == "queued":
                queue.entries[relative] = Entry(
                    source=entry.source,
                    state="legacy",
                    reason=f"Historical state: {old.get('stan')}; [[{note.identifier}]]",
                )
        queue.journal.append(event(f"Imported legacy registry [[{note.identifier}]]"))
        await service.save(queue, previous)
    return {"state": "verified", "source_path": path, "note": note.identifier, "sha256": digest}
