"""PDF originals, not contaminated sidecars, determine published wiki text."""

from pathlib import Path

import pymupdf
import pytest

from basic_memory.wiki_ingest.models import SourceConfig
from basic_memory.wiki_ingest.pdf_review import inspect_page, verify_page
from basic_memory.wiki_ingest.service import IngestService
from tests.wiki_ingest.test_workflow import MemoryStore


@pytest.fixture
def pdf_workflow(tmp_path: Path):
    with pymupdf.open() as pdf:
        for text in ("Original page one", "Original page two"):
            with pymupdf.open() as original:
                original.new_page().insert_text((72, 72), text)
                image = original[0].get_pixmap().tobytes("png")
            page = pdf.new_page()
            page.insert_image(page.rect, stream=image)
            assert not page.get_text()  # Image-only scan, no extractable text layer.
        pdf.save(tmp_path / "scan.pdf")
    (tmp_path / "scan.md").write_text("Wrong OCR\nPERSONAL NOTES: unproven accusation")
    store = MemoryStore()
    return IngestService(store, SourceConfig(documents=str(tmp_path))), store, tmp_path


async def test_sidecar_is_not_published_and_every_original_page_must_be_reviewed(pdf_workflow):
    service, store, root = pdf_workflow
    await service.refresh()
    assert (await service.start("scan.pdf"))["state"] == "needs_pdf_review"
    assert not any(key.startswith("dokumenty/") for key in store.notes)
    candidate = await service.read(0, 40000)
    assert candidate["state"] == "unverified_sidecar"
    assert "PERSONAL NOTES" in candidate["text"]
    with pytest.raises(ValueError, match="visual review"):
        await service.finish([], "No findings")
    queue, _ = await service.load()
    source = queue.entries["scan.pdf"].source
    with pytest.raises(ValueError, match="Inspect"):
        await verify_page(store, service.config, source, 1, "fake", "Original", "Checked")
    image, info = await inspect_page(store, service.config, source, 1)
    assert image.startswith(b"\x89PNG") and info["pages"] == 2
    with pytest.raises(ValueError, match="receipt"):
        await verify_page(store, service.config, source, 1, "fake", "Original", "Checked")
    assert isinstance(info["receipt"], str)
    await verify_page(
        store,
        service.config,
        source,
        1,
        info["receipt"],
        "Original page one",
        "Corrected OCR; removed personal notes absent from PDF",
    )
    assert (await service.start(None))["state"] == "needs_pdf_review"
    _, info = await inspect_page(store, service.config, source, 2)
    assert isinstance(info["receipt"], str)
    await verify_page(
        store,
        service.config,
        source,
        2,
        info["receipt"],
        "Original page two",
        "Restored missing second page",
    )
    resumed = IngestService(store, service.config)
    result = await resumed.start(None)
    published = store.notes[result["note"]].content
    assert "Original page one" in published and "Original page two" in published
    assert "Wrong OCR" not in published and "PERSONAL NOTES" not in published
    assert "PERSONAL NOTES" in (root / "scan.md").read_text()
    await resumed.read(0, 40000)
    assert (await resumed.finish([], "No additional findings"))["state"] == "done"


async def test_review_rejects_changed_sidecar_and_invalid_pages(pdf_workflow):
    service, store, root = pdf_workflow
    await service.refresh()
    await service.start("scan.pdf")
    queue, _ = await service.load()
    source = queue.entries["scan.pdf"].source
    with pytest.raises(ValueError, match="out of range"):
        await inspect_page(store, service.config, source, 3)
    _, info = await inspect_page(store, service.config, source, 1)
    assert isinstance(info["receipt"], str)
    (root / "scan.md").write_text("Different revision")
    with pytest.raises(ValueError, match="Source changed"):
        await verify_page(store, service.config, source, 1, info["receipt"], "Text", "Compared")
    with pytest.raises(ValueError, match="Source changed"):
        await service.start(None)


async def test_remote_mcp_returns_image_and_enforces_review_gate(pdf_workflow, monkeypatch):
    import importlib
    import json
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from fastmcp import Client, FastMCP

    tool = importlib.import_module("basic_memory.mcp.tools.wiki_ingest")
    service, store, _ = pdf_workflow

    @asynccontextmanager
    async def project_client(*args, **kwargs):
        yield object(), SimpleNamespace(external_id="test-project")

    monkeypatch.setattr(tool, "get_project_client", project_client)
    monkeypatch.setattr(tool, "KnowledgeClient", lambda *args: None)
    monkeypatch.setattr(tool, "ApiNoteStore", lambda *args: store)
    monkeypatch.setenv(
        "BASIC_MEMORY_INGEST_SOURCES",
        json.dumps({"test-project": service.config.model_dump(mode="json")}),
    )
    server = FastMCP("pdf-review-test")
    server.tool(tool.wiki_ingest)
    async with Client(server) as client:
        await client.call_tool("wiki_ingest", {"action": "refresh", "project": "legal"})
        await client.call_tool("wiki_ingest", {"action": "start", "project": "legal"})
        result = await client.call_tool(
            "wiki_ingest", {"action": "inspect_page", "project": "legal", "page": 1}
        )
        assert any(block.type == "image" for block in result.content)
        info = json.loads(next(block.text for block in result.content if block.type == "text"))
        verified = await client.call_tool(
            "wiki_ingest",
            {
                "action": "verify_page",
                "project": "legal",
                "page": 1,
                "receipt": info["receipt"],
                "markdown": "Original page one",
                "reason": "Compared original image; removed sidecar commentary",
            },
        )
        assert not verified.is_error
        result = await client.call_tool("wiki_ingest", {"action": "start", "project": "legal"})
        assert "needs_pdf_review" in result.content[0].text
