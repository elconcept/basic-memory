"""Read-only import of legacy snapshots and registries, one file per call."""

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

from pydantic import JsonValue

from basic_memory.wiki_ingest.models import Entry
from basic_memory.wiki_ingest.service import IngestService, raw_body, shard_of
from basic_memory.wiki_ingest.sources import EXCLUDED, source_path

LEGACY_ROOTS = {"00_basic_memory", "00_wiki", "_ingest", ".ingest", "_plany", "_narzedzia"}
REGISTRIES = {"_ingest/rejestr_bm.json", ".ingest/rejestr_bm.json"}


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
    parts = PurePosixPath(path).parts
    if len(parts) < 2 or parts[0] not in LEGACY_ROOTS:
        raise ValueError("Only legacy wiki, memory, ingest and plan snapshots can be imported")
    # The legacy root itself may be hidden (.ingest); everything below it obeys
    # the normal boundary rules.
    file = source_path(str(Path(root) / parts[0]), PurePosixPath(*parts[1:]).as_posix())
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
    result: dict[str, JsonValue] = {
        "state": "verified",
        "source_path": path,
        "note": note.identifier,
        "sha256": digest,
    }
    if path in REGISTRIES:
        state, previous = await service.load()
        if state.active:
            raise ValueError("Finish active document before importing legacy registry")
        shards = await service.load_shards()
        if not any(entries for entries, _ in shards.values()):
            raise ValueError("Inventory is empty; refresh until complete before importing")
        legacy = json.loads(text)
        touched: dict[int, dict[str, Entry]] = {}
        for old_path, old in legacy["dokumenty"].items():
            # Only documents the old tooling actually processed; 'kolejka' was
            # never read and 'usuniety' no longer exists.
            if old.get("stan") in {None, "kolejka", "usuniety"}:
                continue
            relative = old_path.removeprefix("Dokumentacja/")
            index = shard_of(relative)
            entries = shards[index][0]
            entry = entries.get(relative)
            if entry is not None and entry.state == "queued":
                entry.state = "legacy"
                entry.reason = f"Historical state: {old.get('stan')}; [[{note.identifier}]]"
                touched[index] = entries
        for index, entries in touched.items():
            await service.save_shard(index, entries, shards[index][1])
        marked = sum(e.state == "legacy" for t in touched.values() for e in t.values())
        service.record(state, None, f"Imported legacy registry [[{note.identifier}]]: {marked}")
        await service.save(state, previous)
        result["marked_legacy"] = marked
    return result
