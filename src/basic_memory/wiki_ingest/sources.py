"""Read source mounts without depending on a client's filesystem or current directory."""

import hashlib
import os
from pathlib import Path, PurePosixPath

from basic_memory.wiki_ingest.models import Source, SourceConfig

EXCLUDED = {"@eaDir", "#recycle", "#snapshot", "archive", "temp", "node_modules"}
IGNORED_FILES = {"Thumbs.db", "Icon", "Icon\r", "_DUZE_PLIKI.json", "_bez_tekstu.txt"}


def source_path(root: str, relative: str) -> Path:
    """Reject traversal and symlink escapes before opening a source."""
    rel = PurePosixPath(relative)
    if not relative or rel.is_absolute() or ".." in rel.parts or "\\" in relative:
        raise ValueError("Source path must be relative to its configured mount")
    if any(p in EXCLUDED or p.startswith(".") for p in rel.parts):
        raise ValueError("Metadata and hidden paths are not ingest sources")
    base = Path(root).resolve(strict=True)
    path = base.joinpath(*rel.parts).resolve(strict=True)
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError("Source escapes its configured mount or is not a file")
    return path


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot(config: SourceConfig, relative: str) -> Source:
    path = source_path(config.documents, relative)
    sidecar = None
    if path.suffix.lower() in {".md", ".txt"}:
        sidecar = relative
    else:
        # Existing .md is authoritative, including an empty one. Never silently
        # replace a failed/empty sidecar with a weaker extraction.
        candidates = [
            str(PurePosixPath(relative).with_suffix(".md")),
            relative + ".md",
            str(PurePosixPath(relative).with_suffix(".txt")),
        ]
        for candidate in candidates:
            if (Path(config.documents) / candidate).exists():
                source_path(config.documents, candidate)
                sidecar = candidate
                break
    return Source(
        path=relative,
        sha256=checksum(path),
        size=path.stat().st_size,
        sidecar=sidecar,
        sidecar_sha256=checksum(source_path(config.documents, sidecar)) if sidecar else None,
    )


def inventory(config: SourceConfig) -> dict[str, Source]:
    root = Path(config.documents).resolve(strict=True)
    result: dict[str, Source] = {}

    def fail(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, onerror=fail, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in EXCLUDED
            and not d.startswith(".")
            and not (Path(directory) / d).is_symlink()
        )
        names = {
            f
            for f in files
            if f not in IGNORED_FILES
            and not f.startswith((".", "~$"))
            and not f.endswith(".contract.json")
        }
        sidecars = set()
        for name in names:
            if Path(name).suffix.lower() not in {".md", ".txt"}:
                sidecars.update(
                    {
                        str(Path(name).with_suffix(".md")),
                        name + ".md",
                        str(Path(name).with_suffix(".txt")),
                    }
                )
        for name in sorted(names - sidecars):
            rel = (Path(directory) / name).relative_to(root).as_posix()
            result[rel] = snapshot(config, rel)
    return result


def read_text(config: SourceConfig, source: Source) -> str:
    if source.sidecar is None:
        raise ValueError("Sidecar missing: call wiki_ingest action=extract")
    path = source_path(config.documents, source.sidecar)
    with path.open("rb") as stream:
        raw = stream.read(config.max_text_bytes + 1)
    if len(raw) > config.max_text_bytes:
        raise ValueError("Sidecar exceeds configured max_text_bytes")
    if hashlib.sha256(raw).hexdigest() != source.sidecar_sha256:
        raise ValueError("Sidecar changed during read; refresh the inventory")
    text = raw.decode("utf-8")
    if not text.strip():
        raise ValueError("Empty sidecar: review the original before continuing")
    return text
