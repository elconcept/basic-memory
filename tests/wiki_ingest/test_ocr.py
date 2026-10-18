"""Exercise the OCR wire protocol and exclusive sidecar publication."""

from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from pydantic import JsonValue

from basic_memory.wiki_ingest import ocr
from basic_memory.wiki_ingest.models import SourceConfig
from basic_memory.wiki_ingest.service import IngestService
from tests.wiki_ingest.test_workflow import MemoryStore


@pytest.mark.parametrize("upload", [False, True])
async def test_resumable_ocr_pagination_and_capture(tmp_path, monkeypatch, upload):
    (tmp_path / "scan.pdf").write_bytes(b"original")
    server = FastMCP("ocr-test")
    calls = []

    @server.tool
    def parse_document(
        response_mode: str,
        wait_seconds: int,
        path: str | None = None,
        filename: str | None = None,
        content_base64: str | None = None,
    ) -> dict[str, JsonValue]:
        calls.append(path)
        if upload:
            import base64

            assert filename == "scan.pdf"
            assert content_base64 is not None
            assert base64.b64decode(content_base64) == b"original"
        return {"job_id": "job-1", "status": "running", "done": False}

    @server.tool
    def get_job_status(job_id: str, wait_seconds: int) -> dict[str, JsonValue]:
        return {"job_id": job_id, "status": "succeeded", "done": True}

    @server.tool
    def get_document_text(
        job_id: str, format: str, offset: int, limit: int
    ) -> dict[str, JsonValue]:
        return {
            "content": "First\n" if offset == 0 else "Last  \n",
            "next_offset": 6 if offset == 0 else None,
            "complete": offset != 0,
        }

    monkeypatch.setattr(ocr, "Client", lambda url, **kwargs: Client(server))
    service = IngestService(
        MemoryStore(),
        SourceConfig(
            documents=str(tmp_path),
            ocr_url="http://ocr/mcp",
            ocr_root=None if upload else "/sources",
        ),
    )
    await service.refresh()
    assert (await service.start(None))["state"] == "needs_extraction"
    assert (await ocr.extract(service))["state"] == "extracting"
    assert (await ocr.extract(service))["state"] == "extracted"
    assert (tmp_path / "scan.pdf.md").read_bytes() == b"First\nLast  \n"
    assert calls == [None if upload else "/sources/scan.pdf"]
    assert (await ocr.extract(service))["state"] == "sidecar_available"
    await service.start(None)
    assert (await service.read(0, 40000))["text"] == "First\nLast  \n"


def test_sidecar_publish_never_overwrites(tmp_path: Path):
    target = tmp_path / "scan.pdf.md"
    ocr.publish_sidecar(target, "first")
    with pytest.raises(FileExistsError):
        ocr.publish_sidecar(target, "replacement")
    assert target.read_text() == "first"
    assert list(tmp_path.iterdir()) == [target]


async def test_missing_configured_ocr_token(tmp_path):
    (tmp_path / "scan.pdf").write_bytes(b"original")
    service = IngestService(
        MemoryStore(),
        SourceConfig(
            documents=str(tmp_path),
            ocr_url="http://ocr/mcp",
            ocr_token_env="MISSING_INGEST_TEST_TOKEN",
        ),
    )
    await service.refresh()
    await service.start(None)
    with pytest.raises(ValueError, match="token environment"):
        await ocr.extract(service)
