"""Scheduled inventory contract: source listing is offloaded to a DSM job."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from basic_memory.wiki_ingest.models import SourceConfig
from basic_memory.wiki_ingest.sources import listing


@pytest.fixture
def scanner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_document_inventory.py"
    spec = importlib.util.spec_from_file_location("build_document_inventory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_excludes_video_frames_and_tracks_moves(tmp_path, scanner):
    docs = tmp_path / "docs"
    docs.mkdir()
    video = docs / "33_WIDEO" / "WIDEO_A2"
    video.mkdir(parents=True)
    (video / "frame_00001.jpg").write_bytes(b"frame")
    (video / "ramka_00001.jpg").write_bytes(b"frame")
    (video / "analysis.md").write_text("analysis")
    (docs / "old.pdf").write_bytes(b"pdf")
    first = scanner.build(docs)
    assert "old.pdf" in first["files"]
    assert "33_WIDEO/WIDEO_A2/analysis.md" in first["files"]
    assert not any(path.endswith(".jpg") for path in first["files"])
    (docs / "old.pdf").rename(docs / "new.pdf")
    later = scanner.build(docs, first)
    assert later["relocations"] == [{
        "from": "old.pdf", "to": "new.pdf",
        "identity": later["files"]["new.pdf"]["identity"],
    }]


def test_server_reads_only_inventory_and_rejects_stale(tmp_path, scanner, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "brief.md").write_text("brief")
    index = tmp_path / "index.json"
    scanner.atomic_write(index, scanner.build(docs))
    config = SourceConfig(documents=str(docs), inventory_index=str(index),
                          inventory_expected_root=str(docs.resolve()))
    monkeypatch.setattr("basic_memory.wiki_ingest.sources.os.walk",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("walk")))
    assert list(listing(config)) == ["brief.md"]
    data = json.loads(index.read_text())
    data["generated_at"] = (datetime.now(timezone.utc)-timedelta(days=5)).isoformat()
    scanner.atomic_write(index, data)
    with pytest.raises(ValueError, match="stale"):
        listing(config)
