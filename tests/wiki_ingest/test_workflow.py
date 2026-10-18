"""Behavioral tests of the complete source-to-findings protocol."""

import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import JsonValue

from basic_memory.wiki_ingest.migration import migrate
from basic_memory.wiki_ingest.models import SourceConfig
from basic_memory.wiki_ingest.service import IngestService
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
