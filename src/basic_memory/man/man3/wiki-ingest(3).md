---
title: wiki-ingest(3)
type: manpage
section: 3
name: wiki-ingest
summary: ingest full sources into a resumable, verified wiki workflow
generated: registry
tool: wiki_ingest
---

# wiki-ingest(3)

## NAME

**wiki-ingest** — ingest full sources into a resumable, verified wiki workflow

## SYNOPSIS

```
wiki_ingest(action, project, path=None, offset=0, limit=12000,
            related_notes=None, reason="", page=1, receipt="", markdown="", project_id=None)
```

## PARAMETERS

- **action** (string, required)
- **project** (string, required)
- **path** (string | null, optional, default: None)
- **offset** (integer, optional, default: 0)
- **limit** (integer, optional, default: 12000)
- **related_notes** (array | null, optional, default: None)
- **reason** (string, optional, default: "")
- **page** (integer, 1-based): original PDF page for inspection/verification
- **receipt** (string): receipt returned with the inspected page image
- **markdown** (string): corrected page transcription, excluding sidecar commentary
- **project_id** (string | null, optional, default: None)

## DESCRIPTION

The server owns source mounts, queue state and mechanical verification. The calling
model reads every source and writes its interpretation using ordinary note tools.
Works over remote MCP without a client filesystem, Python, shell, or host hooks.

1. `status` resumes the active source. `refresh` inventories new/changed sources,
   rehashing only files whose size/mtime changed. It reconciles inventory shards
   within a time budget; repeat it while it returns `complete: false`.
2. `start(path=...)` reserves one document. PDFs return `needs_pdf_review` without
   publishing the sidecar. `read` returns the candidate as `unverified_sidecar`.
   Call `inspect_page(page=N)` for every original PDF page: it returns an image,
   total page count and a receipt. Compare the image against the sidecar, correct
   OCR and omissions, and exclude commentary absent from the PDF. Call
   `verify_page(page=N, receipt=..., markdown=..., reason=...)` with the corrected
   transcription and concrete comparison evidence. Mark illegible fragments
   explicitly; represent blank pages as `[blank page]`. Preserve notes separately.
   After all pages are verified, call `start` again to publish corrected Markdown
   under `dokumenty/`. Non-PDF sources retain the full-sidecar capture behavior.
   Omit path to select the next queued source. Revisions remain separate snapshots.
3. If `needs_extraction`, call `extract`. It starts or polls the configured the-ocr
   service, gathers every page of returned Markdown and creates a missing sidecar.
   Repeat while `extracting`, then `start` to capture the text. Existing sidecars
   are never overwritten. OCR failure leaves the document active.
4. `read(offset=next_offset)` until `complete=true`. Offsets count characters,
   not bytes or PDF pages. Source text and pagination are returned verbatim.
5. Write findings/people/events/case notes with `write_note`/`edit_note`. Every
   findings note must link to the exact source note returned by `start`.
   Record actual page/block anchors, disagreements and uncertainty in the notes.
6. `audit(related_notes=[...])` reports dates, times, amounts, case numbers and
   page references missing from findings. This assists review; it is not a legal
   or semantic completeness judgment.
7. `finish(related_notes=[...])` checks current source hashes, complete reading,
   exact full-text preservation and source links in findings. A document with no
   new findings requires a nonempty `reason`. `skip` requires a reason and records
   an omission, never successful ingest.
8. `index` rebuilds the source/findings navigation note. The ordinary Basic Memory
   graph and wiki projector continue to index the accepted notes.

`status` and `migrate` list at most 100 entries per call; follow `next_offset` until
`complete`. `migrate` without path lists historical files; with path copies one
file verbatim to `_ingest/legacy/` and verifies the read-back. It never overwrites
current knowledge. Importing `_ingest/rejestr_bm.json` or `.ingest/rejestr_bm.json`
after a complete `refresh` marks queued sources the old tooling processed as `legacy`,
requiring verification instead of trusting historical completion flags. Legacy
snapshots remain unchanged on disk.

The active source, its read offsets and the recent journal live in the small note
`_ingest/wiki-ingest-state`; `read` rewrites only that note. The inventory lives in
64 hash shards `_ingest/inventory/shard-NN`, each entry with its own history. A
version 1 state note is split into shards on first load; its full journal is kept
in `_ingest/wiki-ingest-journal-v1`. Ordinary
checksum-guarded note writes arbitrate concurrent callers. A failed or ambiguous
call must be followed by `status`; do not blindly repeat mutations.

## CONFIGURATION

`BASIC_MEMORY_INGEST_SOURCES` is a server environment JSON object keyed by **project
UUID** from `list_memory_projects`, never a caller-supplied root or project name:

```json
{"PROJECT_UUID":{"documents":"/mnt/documents","legacy_workspace":"/mnt/legacy","ocr_url":"http://ocr:8000/mcp","ocr_root":"/mnt/documents","ocr_prefix":"","priority":[],"max_text_bytes":20971520}}
```

`documents` is the source mount visible to Basic Memory; `ocr_root` is the same
corpus as seen by the OCR service. `legacy_workspace` is optional and read-only.
Mount original sources read-only if extraction is handled upstream; otherwise
grant the service permission to create sidecars. Credentials and MCP authentication
are deployment configuration, not note content. Each routed project's UUID must
have its own mapping. No mapping means no source access.

## GOTCHAS

- The server enforces delivery and explicit review of every PDF page, bound to
  PDF and sidecar hashes. It cannot prove the reviewer's semantic accuracy.
- PyMuPDF renders original pages; image-capable MCP clients are required for PDF
  review. If images cannot be read, stop before publishing; do not attest review.
- Review drafts and comparison evidence live in `_ingest/pdf-review/`; original
  sidecar files remain unchanged. Existing completed imports are not retroactively
  corrected: affected documents need an explicit repair/re-ingest workflow.
- Source changes invalidate completion. Explain with `skip`, then `refresh` and
  restart against the new version; preserve earlier findings for comparison.
- Interpretation and citation checking remain model/skill work, not extraction.
- This tool does not launch another model or a background agent.
- Recaps belong in the chosen session project and still require a verifying read.

## SEE ALSO

- see_also [[wiki-plan(3)]]
- see_also [[write-note(3)]]
- see_also [[schema-validate(3)]]

After a lost read response, call start again and repeat from resume_offset. The last chunk may have been recorded before delivery to the client.

OCR authentication: set ocr_token_env to the name of a server environment variable
containing the OCR Bearer token. Omitting ocr_root uploads the source over MCP instead
of requiring a shared source mount. max_source_bytes defaults to 100 MiB.
