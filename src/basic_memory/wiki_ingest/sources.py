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


def sidecar_candidates(name: str) -> tuple[str, str, str]:
    return (
        str(PurePosixPath(name).with_suffix(".md")),
        name + ".md",
        str(PurePosixPath(name).with_suffix(".txt")),
    )


def snapshot(config: SourceConfig, relative: str) -> Source:
    path = source_path(config.documents, relative)
    sidecar = None
    if path.suffix.lower() in {".md", ".txt"}:
        sidecar = relative
    else:
        # Existing .md is a candidate transcription, including an empty one.
        # Preserve it for comparison; PDF publication requires visual review.
        for candidate in sidecar_candidates(relative):
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


def listing(config: SourceConfig) -> dict[str, str]:
    """All sources with a cheap stamp (size/mtime of source and paired sidecar).

    No content is read. The stamp only decides whether refresh must rehash; it is
    never evidence of content identity.
    """
    root = Path(config.documents).resolve(strict=True)
    result: dict[str, str] = {}

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
                sidecars.update(sidecar_candidates(name))
        for name in sorted(names - sidecars):
            parts = [name]
            if Path(name).suffix.lower() not in {".md", ".txt"}:
                # Same candidate order as snapshot().
                parts += [c for c in sidecar_candidates(name) if c in files][:1]
            stamp = []
            for item in parts:
                info = (Path(directory) / item).stat()
                stamp.append(f"{item}:{info.st_size}:{info.st_mtime_ns}")
            result[(Path(directory) / name).relative_to(root).as_posix()] = "|".join(stamp)
    return result


def inventory(config: SourceConfig) -> dict[str, Source]:
    return {path: snapshot(config, path) for path in listing(config)}


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
