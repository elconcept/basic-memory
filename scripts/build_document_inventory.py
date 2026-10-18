#!/usr/bin/env python3
"""Atomic, scheduled inventory of a NAS document tree for Basic Memory.

Run from DSM Task Scheduler (e.g. every 15 minutes). No source file contents
are read, no OCR is run, and this script never performs an LLM ingest.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_DIRS = {"@eaDir", "#recycle", "#snapshot", "archive", "temp", "node_modules"}
IGNORED_FILES = {"Thumbs.db", "Icon", "Icon\r", "_DUZE_PLIKI.json", "_bez_tekstu.txt"}


def excluded(relative: str) -> bool:
    parts = Path(relative).parts
    if any(x.startswith(".") or x in EXCLUDED_DIRS for x in parts):
        return True
    # Individual video frames are analysis artifacts, not independent documents.
    return (len(parts) >= 3 and parts[0] == "33_WIDEO"
            and relative.lower().endswith((".jpg", ".jpeg", ".png"))
            and parts[-1].lower().startswith(("frame_", "ramka_")))


def sidecars(name: str) -> tuple[str, str, str]:
    p = Path(name)
    return p.with_suffix(".md").name, name + ".md", p.with_suffix(".txt").name


def build(root: Path, previous: dict | None = None) -> dict:
    root = root.resolve(strict=True)
    result: dict[str, dict] = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(x for x in dirs if x not in EXCLUDED_DIRS
                         and not x.startswith(".")
                         and not (Path(directory) / x).is_symlink())
        names = {name for name in files
                 if name not in IGNORED_FILES and not name.startswith((".", "~$"))
                 and not name.endswith(".contract.json")}
        sidecar_names = set()
        for name in names:
            if Path(name).suffix.lower() not in {".md", ".txt"}:
                sidecar_names.update(sidecars(name))
        for name in sorted(names - sidecar_names):
            src = Path(directory) / name
            rel = src.relative_to(root).as_posix()
            if excluded(rel) or src.is_symlink():
                continue
            st = src.stat()
            candidates = [name]
            if src.suffix.lower() not in {".md", ".txt"}:
                candidates += [x for x in sidecars(name) if x in names][:1]
            stamps = []
            for item in candidates:
                item_path = Path(directory) / item
                if item_path.is_symlink():
                    raise ValueError(f"Symlink sidecar forbidden: {item_path}")
                info = item_path.stat()
                stamps.append(f"{item}:{info.st_size}:{info.st_mtime_ns}")
            result[rel] = {
                "stamp": "|".join(stamps),
                "identity": f"{st.st_dev}:{st.st_ino}",
                "size": st.st_size,
            }

    old = (previous or {}).get("files", {})
    removed = {p: e for p, e in old.items() if p not in result}
    added = {p: e for p, e in result.items() if p not in old}
    old_id: dict[str, list[str]] = {}
    for path, item in removed.items():
        old_id.setdefault(str(item["identity"]), []).append(path)
    moved = []
    for path, item in added.items():
        matches = old_id.get(item["identity"], [])
        if len(matches) == 1 and item["size"] == removed[matches[0]].get("size"):
            moved.append({"from": matches[0], "to": path, "identity": item["identity"]})
    return {
        "version": 1,
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": dict(sorted(result.items())),
        "relocations": sorted(moved, key=lambda x: x["from"]),
    }


def atomic_write(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(content, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    root = args.root.resolve(strict=True)
    dest = args.output.resolve(strict=False)
    if dest.is_relative_to(root):
        ap.error("--output must be outside the scanned source tree")
    previous = None
    if dest.exists():
        previous = json.loads(dest.read_text(encoding="utf-8"))
        if previous.get("root") != str(root) or previous.get("version") != 1:
            ap.error("existing index belongs to another root or format")
    result = build(root, previous)
    atomic_write(dest, result)
    print(f"Indexed {len(result['files'])} sources; {len(result['relocations'])} relocations")


if __name__ == "__main__":
    main()
