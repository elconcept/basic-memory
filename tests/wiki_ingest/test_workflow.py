"""Behavioral tests of the complete source-to-findings protocol."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from basic_memory.wiki_ingest import service as service_module
from basic_memory.wiki_ingest.migration import migrate
from basic_memory.wiki_ingest.models import Entry, Queue, SourceConfig
from basic_memory.wiki_ingest.service import STATE_ID, IngestService
from basic_memory.wiki_ingest.sources import inventory, snapshot, source_path
from basic_memory.wiki_ingest.store import Note


class MemoryStore:
    """An optimistic-concurrency note boundary, without a filesystem backdoor."""

    def __init__(self):
        self.notes: dict[str, Note] = {}

    async def read(self, identifier: str) -> Note | None:
        await asyncio.sleep(0)
        return self.notes.get(identifier)

    async def write(
        self,
        title: str,
        directory: str,
        content: str,
        metadata: dict[str, JsonValue],
        expected: Note | None = None,
    ) -> Note:
        identifier = directory.rstrip("/.") + "/" + title
        existing = self.notes.get(identifier)
        if existing != expected:
            raise ValueError("Revision conflict")
        digest = hashlib.sha256((content + repr(metadata)).encode()).hexdigest()
        note = Note(identifier, digest, content, metadata)
        self.notes[identifier] = note
        await asyncio.sleep(0)
        return note


@pytest.fixture
def workflow(tmp_path: Path) -> tuple[IngestService, MemoryStore, Path]:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "first.md").write_bytes(b"  First line\r\n\nSecond line  \n")
    (source / "second.md").write_text("Second document")
    store = MemoryStore()
    return IngestService(store, SourceConfig(documents=str(source))), store, source


async def test_complete_ingest_can_resume_in_another_client(workflow):
    service, store, source = workflow
    await service.refresh()
    started = await service.start("first.md")
    note = await store.read(started["note"])
    assert source.joinpath("first.md").read_bytes().decode() in note.content
    part = await service.read(0, 5)
    resumed = IngestService(store, service.config)
    rest = await resumed.read(part["next_offset"], 40000)
    assert part["text"] + rest["text"] == source.joinpath("first.md").read_bytes().decode()
    findings = await store.write("finding", "events", f"Evidence [[{started['note']}]]", {})
    result = await resumed.finish([findings.identifier], "")
    assert result["state"] == "done"
    assert (await resumed.status(0, 100))["active"] is None
    assert (await resumed.index())["sources"] == 2


async def test_finish_refuses_unread_text_and_missing_findings(workflow):
    service, _, _ = workflow
    await service.refresh()
    await service.start("first.md")
    with pytest.raises(ValueError, match="Full text"):
        await service.finish([], "")
    with pytest.raises(ValueError, match="Read gap"):
        await service.read(10, 10)
    await service.read(0, 40000)
    with pytest.raises(ValueError, match="related findings"):
        await service.finish([], "")
    with pytest.raises(ValueError, match="Missing/distinct"):
        await service.finish(["missing"], "")
    await service.finish([], "Administrative duplicate; no additional findings")


async def test_one_active_document_and_source_change_detection(workflow):
    service, _, source = workflow
    await service.refresh()
    await service.start("first.md")
    with pytest.raises(ValueError, match="active document"):
        await service.start("second.md")
    await service.read(0, 40000)
    (source / "first.md").write_text("Changed")
    with pytest.raises(ValueError, match="Source changed"):
        await service.finish([], "No findings")
    await service.skip("Changed source; restart against new revision")
    await service.refresh()
    assert (await service.status(0, 100))["entries"]["first.md"]["state"] == "changed"


async def test_concurrent_starts_cannot_claim_two_documents(workflow):
    service, _, _ = workflow
    await service.refresh()
    results = await asyncio.gather(
        service.start("first.md"), service.start("second.md"), return_exceptions=True
    )
    assert sum(isinstance(r, ValueError) for r in results) == 1
    state = await service.status(0, 100)
    assert sum(e["state"] == "active" for e in state["entries"].values()) == 1


async def test_readback_and_findings_source_links_are_required(workflow):
    service, store, _ = workflow
    await service.refresh()
    started = await service.start("first.md")
    await service.read(0, 40000)
    finding = await store.write("unlinked", "events", "No provenance", {})
    with pytest.raises(ValueError, match="must link"):
        await service.finish([finding.identifier], "")
    original = store.notes[started["note"]]
    store.notes[started["note"]] = Note(
        original.identifier, "changed", "Truncated", original.metadata
    )
    with pytest.raises(ValueError, match="missing or changed"):
        await service.finish([], "No findings")


def test_inventory_pairs_sidecars_and_excludes_synology_metadata(tmp_path):
    (tmp_path / "file.pdf").write_bytes(b"source")
    (tmp_path / "file.pdf.md").write_text("Full text")
    (tmp_path / "#snapshot").mkdir()
    (tmp_path / "#snapshot" / "ignored.md").write_text("ignore")
    config = SourceConfig(documents=str(tmp_path))
    items = inventory(config)
    assert list(items) == ["file.pdf"]
    assert items["file.pdf"].sidecar == "file.pdf.md"


@pytest.mark.parametrize(
    "path", ["../secret", "/etc/passwd", "#snapshot/file", "a/../../b", ".env"]
)
def test_source_boundaries_reject_unsafe_paths(tmp_path, path):
    with pytest.raises(ValueError):
        source_path(str(tmp_path), path)


def test_source_symlink_cannot_escape_mount(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret").write_text("private")
    (root / "escape").symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="escapes"):
        snapshot(SourceConfig(documents=str(root)), "escape")


async def test_missing_and_empty_sidecars_do_not_silently_fall_back(workflow):
    service, _, source = workflow
    (source / "scan.pdf").write_bytes(b"fake PDF")
    await service.refresh()
    assert (await service.start("scan.pdf"))["state"] == "needs_extraction"
    await service.skip("OCR pending")
    (source / "scan.md").write_text("")
    await service.refresh()
    assert (await service.start("scan.pdf"))["state"] == "needs_pdf_review"
    with pytest.raises(ValueError, match="Empty sidecar"):
        await service.read(0, 40000)


async def test_legacy_snapshot_preserves_original_and_rejects_other_paths(workflow, tmp_path):
    service, store, _ = workflow
    legacy = tmp_path / "legacy"
    (legacy / "00_wiki").mkdir(parents=True)
    raw = "---\ntitle: Old\n---\n\n  Original bytes.\n"
    (legacy / "00_wiki" / "note.md").write_text(raw)
    service.config.legacy_workspace = str(legacy)
    result = await migrate(service, "00_wiki/note.md", 0, 10)
    note = await store.read(result["note"])
    assert raw in note.content
    assert (legacy / "00_wiki" / "note.md").read_text() == raw
    assert (await migrate(service, "00_wiki/note.md", 0, 10))["note"] == result["note"]
    with pytest.raises(ValueError, match="Only legacy"):
        await migrate(service, "secrets/key", 0, 10)


async def test_start_recovers_after_source_note_write_lost_response(workflow):
    service, store, _ = workflow
    await service.refresh()
    original = store.write
    failed = False

    async def lost_response(title, directory, content, metadata, expected=None):
        nonlocal failed
        note = await original(title, directory, content, metadata, expected)
        if directory.startswith("dokumenty") and not failed:
            failed = True
            raise ConnectionError("Response lost after commit")
        return note

    store.write = lost_response
    with pytest.raises(ConnectionError):
        await service.start("first.md")
    result = await service.start(None)
    assert result["state"] == "active"
    assert len([k for k in store.notes if k.startswith("dokumenty/")]) == 1


async def test_reads_rewrite_only_the_small_state_note(workflow):
    service, store, source = workflow
    for i in range(300):
        (source / f"bulk-{i:03d}.md").write_text(f"Document {i}")
    await service.refresh()
    await service.start("first.md")
    writes: list[str] = []
    original = store.write

    async def tracking(title, directory, content, metadata, expected=None):
        writes.append(f"{directory}/{title}")
        return await original(title, directory, content, metadata, expected)

    store.write = tracking
    await service.read(0, 5)
    await service.read(5, 40000)
    assert writes == ["_ingest/wiki-ingest-state"] * 2
    assert len(repr(store.notes[STATE_ID].metadata)) < 5000


async def test_version_1_state_is_split_into_shards_without_losing_progress(workflow):
    service, store, _ = workflow
    entries = {p: Entry(source=snapshot(service.config, p)) for p in ("first.md", "second.md")}
    entries["first.md"].state = "active"
    entries["first.md"].next_offset = 3
    legacy = Queue(entries=entries, active="first.md", journal=["old event"])
    await store.write(
        "wiki-ingest-state", "_ingest", "v1", {"wiki_ingest": legacy.model_dump(mode="json")}
    )
    state, _ = await service.load()
    assert state.active == "first.md" and state.entry.next_offset == 3
    assert "old event" in store.notes["_ingest/wiki-ingest-journal-v1"].content
    status = await service.status(0, 10)
    assert status["total"] == 2 and status["entries"]["second.md"]["state"] == "queued"


async def test_refresh_reuses_hashes_and_resumes_after_deadline(workflow, monkeypatch):
    service, _, source = workflow
    for i in range(5):
        (source / f"more-{i}.md").write_text(f"More {i}")
    hashed: list[str] = []
    real = service_module.snapshot

    def counting(config, relative):
        hashed.append(relative)
        return real(config, relative)

    monkeypatch.setattr(service_module, "snapshot", counting)
    monkeypatch.setattr(service_module, "REFRESH_SECONDS", -1.0)
    first = await service.refresh()
    # An exhausted budget still reconciles one shard, so every call progresses.
    assert first["complete"] is False and first["pending_shards"] == service_module.SHARDS - 1
    calls = 1
    while not (await service.refresh())["complete"]:
        calls += 1
    assert calls + 1 == service_module.SHARDS
    assert sorted(hashed) == sorted((await service.status(0, 100))["entries"])
    assert len(hashed) == 7
    monkeypatch.setattr(service_module, "REFRESH_SECONDS", 40.0)
    hashed.clear()
    assert (await service.refresh())["complete"] is True
    assert hashed == []
    (source / "first.md").write_text("Edited")
    (source / "second.md").unlink()
    await service.refresh()
    assert hashed == ["first.md"]
    entries = (await service.status(0, 100))["entries"]
    assert entries["first.md"]["state"] == "changed"
    assert entries["second.md"]["state"] == "missing"


async def test_hidden_legacy_registry_marks_only_processed_documents(workflow, tmp_path):
    service, _, _ = workflow
    legacy = tmp_path / "legacy"
    (legacy / ".ingest").mkdir(parents=True)
    registry = {
        "dokumenty": {
            "Dokumentacja/first.md": {"stan": "zrobiony"},
            "Dokumentacja/second.md": {"stan": "kolejka"},
        }
    }
    (legacy / ".ingest" / "rejestr_bm.json").write_text(json.dumps(registry))
    service.config.legacy_workspace = str(legacy)
    listed = await migrate(service, None, 0, 10)
    assert listed["paths"] == [".ingest/rejestr_bm.json"]
    with pytest.raises(ValueError, match="Inventory is empty"):
        await migrate(service, ".ingest/rejestr_bm.json", 0, 10)
    await service.refresh()
    result = await migrate(service, ".ingest/rejestr_bm.json", 0, 10)
    assert result["marked_legacy"] == 1
    entries = (await service.status(0, 10))["entries"]
    assert entries["first.md"]["state"] == "legacy"
    assert entries["second.md"]["state"] == "queued"
    with pytest.raises(ValueError):
        await migrate(service, ".ingest/../.env", 0, 10)



async def test_shard_reads_are_bounded_to_prevent_sqlite_pool_exhaustion(workflow, monkeypatch):
    """A status call may inspect 64 shards, but not occupy 64 DB sessions."""
    service, _, _ = workflow
    active = 0
    maximum = 0
    original = service.load_shard

    async def observed(index):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.001)
            return await original(index)
        finally:
            active -= 1

    monkeypatch.setattr(service, "load_shard", observed)
    result = await service.load_shards()

    assert set(result) == set(range(service_module.SHARDS))
    assert 1 < maximum <= service_module.SHARD_READ_BATCH
    assert active == 0
