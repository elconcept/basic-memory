"""Page-by-page visual review, bound to PDF and sidecar revisions.

The server proves page delivery and explicit review, not semantic correctness.
Only the reviewer can compare the rendered image with the corrected transcription.
"""

import asyncio
import hashlib
import secrets
from pathlib import PurePosixPath

import pymupdf
from pydantic import BaseModel, JsonValue

from basic_memory.wiki_ingest.models import Source, SourceConfig
from basic_memory.wiki_ingest.sources import snapshot, source_path
from basic_memory.wiki_ingest.store import NoteStore


class PageReview(BaseModel):
    source: Source
    page: int
    pages: int
    receipt: str
    markdown: str | None = None
    evidence: str = ""


def review_id(source: Source, page: int) -> str:
    digest = hashlib.sha256(source.model_dump_json().encode()).hexdigest()
    return f"_ingest/pdf-review/{digest}-{page}"


def render_page(config: SourceConfig, source: Source, page: int) -> tuple[bytes, int]:
    if PurePosixPath(source.path).suffix.lower() != ".pdf":
        raise ValueError("Visual page review requires a PDF")
    if source.size > config.max_source_bytes:
        raise ValueError("PDF exceeds configured max_source_bytes")
    with pymupdf.open(source_path(config.documents, source.path)) as pdf:
        if pdf.needs_pass or not 1 <= page <= len(pdf):
            raise ValueError("PDF is encrypted or page is out of range")
        sheet = pdf[page - 1]
        # Bound image memory even for pathological page dimensions.
        scale = min(2.0, 2400 / max(sheet.rect.width, sheet.rect.height))
        image = sheet.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
        return image, len(pdf)


async def inspect_page(
    store: NoteStore, config: SourceConfig, source: Source, page: int
) -> tuple[bytes, dict[str, JsonValue]]:
    image, pages = await asyncio.to_thread(render_page, config, source, page)
    if await asyncio.to_thread(snapshot, config, source.path) != source:
        raise ValueError("Source changed during PDF inspection")
    identifier = review_id(source, page)
    old = await store.read(identifier)
    review = (
        PageReview.model_validate(old.metadata["pdf_review"])
        if old
        else PageReview(source=source, page=page, pages=pages, receipt=secrets.token_hex(16))
    )
    if old is None:
        await store.write(
            identifier.rsplit("/", 1)[1],
            "_ingest/pdf-review",
            "PDF page delivered for review; transcription not yet accepted.",
            {"type": "pdf_review", "pdf_review": review.model_dump(mode="json")},
        )
    return image, {
        "page": page,
        "pages": pages,
        "receipt": review.receipt,
        "source_sha256": source.sha256,
        "sidecar_sha256": source.sidecar_sha256,
        "next": "Compare image with sidecar; verify_page with corrected markdown and evidence",
    }


async def verify_page(
    store: NoteStore,
    config: SourceConfig,
    source: Source,
    page: int,
    receipt: str,
    markdown: str,
    evidence: str,
) -> dict[str, JsonValue]:
    if not markdown.strip() or not evidence.strip():
        raise ValueError("Supply corrected transcription and concrete comparison evidence")
    if len(markdown.encode()) > config.max_text_bytes:
        raise ValueError("Transcription exceeds configured max_text_bytes")
    if await asyncio.to_thread(snapshot, config, source.path) != source:
        raise ValueError("Source changed during PDF review")
    identifier = review_id(source, page)
    old = await store.read(identifier)
    if old is None:
        raise ValueError("Inspect the original PDF page before verifying it")
    review = PageReview.model_validate(old.metadata["pdf_review"])
    if review.source != source or not secrets.compare_digest(review.receipt, receipt):
        raise ValueError("Review receipt does not match the inspected source revision")
    review.markdown = markdown
    review.evidence = evidence
    await store.write(
        identifier.rsplit("/", 1)[1],
        "_ingest/pdf-review",
        markdown,
        {"type": "pdf_review", "pdf_review": review.model_dump(mode="json")},
        old,
    )
    return {"state": "page_verified", "page": page, "review": identifier}


async def reviewed_text(store: NoteStore, source: Source) -> str | None:
    first = await store.read(review_id(source, 1))
    if first is None:
        return None
    first_review = PageReview.model_validate(first.metadata["pdf_review"])
    pages = []
    for page in range(1, first_review.pages + 1):
        note = await store.read(review_id(source, page))
        if note is None:
            return None
        review = PageReview.model_validate(note.metadata["pdf_review"])
        if review.source != source or review.page != page or review.pages != first_review.pages:
            raise ValueError("PDF review identity mismatch")
        if review.markdown is None:
            return None
        if note.content.rstrip() != review.markdown.rstrip():
            # API-owned frontmatter may prefix accepted Markdown.
            if not note.content.rstrip().endswith("\n" + review.markdown.rstrip()):
                raise ValueError(
                    "PDF transcription review was modified outside the review workflow"
                )
        pages.append(f"## Strona {page}\n\n{review.markdown}")
    return "\n\n".join(pages)
