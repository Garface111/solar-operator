"""The document vault: ingestion + text extraction for the household's records.

Originals are kept on disk under documents/ (gitignored, next to the DB); extracted
text lives in the Document row so the copilot can reread and search everything.
Supported extraction: PDF (pypdf), .docx, and any plain-text format. Scanned-image
PDFs without a text layer extract poorly — the copilot is told when text is thin.
"""
from __future__ import annotations

import hashlib
import html
import io
import re
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import Document

DOCUMENTS_DIR = config.BASE_DIR / "documents"
MAX_TEXT_CHARS = 500_000

# Ingestion caps so a hostile attachment can't exhaust memory. The web upload
# route caps bytes too, but the inbound-email path reaches add_document without
# one — this is the single choke point every path passes through. A file or a
# decompressed member over the limit is refused (the caller files it without
# extracted text) rather than materialized in full and regex-scanned.
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024      # raw file size
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024      # decompressed word/document.xml (zip-bomb guard)
MAX_PDF_PAGES = 500                        # bound pypdf work on a crafted PDF

CATEGORIES = [
    "home", "contract", "insurance", "estate", "tax", "identity", "financial", "other",
]

_DOCX_TEXT = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)
_DOCX_PARA = re.compile(r"</w:p>")


#: Images hold no text layer. Decoding their bytes as UTF-8 produces pages of
#: replacement characters that look like content and poison search, so they are
#: stored with empty text — the copilot reads the file itself instead.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp")


def is_image(filename: str) -> bool:
    return filename.lower().endswith(IMAGE_EXTENSIONS)


def extract_text(filename: str, data: bytes) -> str:
    lower = filename.lower()
    if is_image(lower):
        return ""
    try:
        if lower.endswith(".pdf"):
            return _extract_pdf(data)
        if lower.endswith(".docx"):
            return _extract_docx(data)
    except Exception:
        return ""
    return data.decode("utf-8", errors="replace")


def stored_path(doc: Document):
    """Where the original file lives on disk, or None if it is missing."""
    for path in DOCUMENTS_DIR.glob(f"{doc.id}__*"):
        return path
    return None


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages):
        if i >= MAX_PDF_PAGES:
            pages.append(f"[document truncated at {MAX_PDF_PAGES} pages]")
            break
        text = page.extract_text() or ""
        pages.append(f"[page {i + 1}]\n{text}")
    return "\n\n".join(pages)


def _extract_docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Refuse a decompression bomb: check the declared uncompressed size, then
        # read with a hard byte budget so a lying header can't slip past either.
        info = zf.getinfo("word/document.xml")
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise ValueError("docx document.xml exceeds the size limit")
        with zf.open(info) as fh:
            raw = fh.read(MAX_DOCX_XML_BYTES + 1)
        if len(raw) > MAX_DOCX_XML_BYTES:
            raise ValueError("docx document.xml exceeds the size limit")
        xml = raw.decode("utf-8", errors="replace")
    xml = _DOCX_PARA.sub("\n", xml)
    parts = [html.unescape(m) for m in _DOCX_TEXT.findall(xml)]
    return re.sub(r"\n{3,}", "\n\n", "".join(
        p if p else "" for p in parts
    )) or ""


def add_document(
    session: Session,
    *,
    filename: str,
    data: bytes,
    title: str | None = None,
    category: str = "other",
) -> tuple[Document, bool]:
    """Store a document; returns (document, created). Same bytes twice = no-op."""
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"file exceeds the {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB limit")
    digest = hashlib.sha256(data).hexdigest()
    existing = session.execute(
        select(Document).where(Document.sha256 == digest)
    ).scalar_one_or_none()
    if existing:
        return existing, False
    text = extract_text(filename, data)[:MAX_TEXT_CHARS]
    doc = Document(
        title=(title or Path(filename).stem or "Untitled").strip()[:200],
        category=category if category in CATEGORIES else "other",
        filename=Path(filename).name,
        sha256=digest,
        size_bytes=len(data),
        content_text=text,
    )
    session.add(doc)
    session.flush()
    DOCUMENTS_DIR.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", Path(filename).name) or "file"
    (DOCUMENTS_DIR / f"{doc.id}__{safe_name}").write_bytes(data)
    return doc, True


def delete_document(session: Session, doc: Document) -> None:
    for path in DOCUMENTS_DIR.glob(f"{doc.id}__*"):
        path.unlink(missing_ok=True)
    session.delete(doc)


def search_documents(session: Session, query: str, context: int = 240) -> list[dict]:
    """Case-insensitive substring search across all document text; returns snippets."""
    needle = query.lower().strip()
    results: list[dict] = []
    if not needle:
        return results
    for doc in session.execute(select(Document)).scalars():
        haystack = doc.content_text.lower()
        snippets = []
        start = 0
        while len(snippets) < 3:
            idx = haystack.find(needle, start)
            if idx == -1:
                break
            lo, hi = max(0, idx - context // 2), idx + len(needle) + context // 2
            snippets.append("…" + doc.content_text[lo:hi].replace("\n", " ") + "…")
            start = idx + len(needle)
        if snippets:
            results.append(
                {"document_id": doc.id, "title": doc.title, "category": doc.category,
                 "matches": snippets}
            )
    return results
