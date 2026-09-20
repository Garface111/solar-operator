import io
import json
import zipfile

import pytest

from bankai import vault
from bankai.agent.tools import READ_PAGE_CHARS, execute_tool
from bankai.models import Document


@pytest.fixture(autouse=True)
def documents_dir(tmp_path, monkeypatch):
    d = tmp_path / "documents"
    monkeypatch.setattr(vault, "DOCUMENTS_DIR", d)
    return d


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    xml = (
        '<?xml version="1.0"?><w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


# --- extraction ---

def test_extract_plaintext():
    assert "mortgage note" in vault.extract_text("note.txt", b"the mortgage note text")


def test_extract_docx():
    data = make_docx(["Purchase and Sale Agreement", "The closing date is June 1, 2024."])
    text = vault.extract_text("contract.docx", data)
    assert "Purchase and Sale Agreement" in text
    assert "closing date is June 1, 2024" in text


def test_extract_pdf_page_markers():
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    text = vault.extract_text("deed.pdf", buf.getvalue())
    assert "[page 1]" in text


def test_extract_corrupt_file_returns_empty():
    assert vault.extract_text("broken.docx", b"not a zip at all") == ""


# --- vault storage ---

def test_add_document_stores_text_and_original(session, documents_dir):
    doc, created = vault.add_document(
        session, filename="deed.txt", data=b"Warranty deed for 12 Maple St",
        title="House deed", category="home",
    )
    assert created
    assert doc.category == "home"
    assert "12 Maple St" in doc.content_text
    on_disk = list(documents_dir.glob(f"{doc.id}__*"))
    assert len(on_disk) == 1
    assert on_disk[0].read_bytes() == b"Warranty deed for 12 Maple St"


def test_add_document_dedupes_by_sha256(session):
    d1, created1 = vault.add_document(session, filename="a.txt", data=b"same bytes")
    d2, created2 = vault.add_document(session, filename="b.txt", data=b"same bytes")
    assert created1 and not created2
    assert d1.id == d2.id
    assert session.query(Document).count() == 1


def test_add_document_defaults(session):
    doc, _ = vault.add_document(
        session, filename="scan.txt", data=b"x", category="not-a-category"
    )
    assert doc.title == "scan"
    assert doc.category == "other"


def test_delete_document_removes_row_and_file(session, documents_dir):
    doc, _ = vault.add_document(session, filename="w.txt", data=b"will text")
    assert list(documents_dir.glob(f"{doc.id}__*"))
    vault.delete_document(session, doc)
    session.flush()
    assert session.query(Document).count() == 0
    assert not list(documents_dir.glob("*"))


def test_search_documents_snippets(session):
    vault.add_document(
        session, filename="policy.txt",
        data=b"Homeowners policy HO-3. The deductible is $2,500 per occurrence.",
        title="Home insurance", category="insurance",
    )
    vault.add_document(session, filename="other.txt", data=b"nothing relevant here")
    results = vault.search_documents(session, "deductible")
    assert len(results) == 1
    assert results[0]["title"] == "Home insurance"
    assert "$2,500" in results[0]["matches"][0]
    assert vault.search_documents(session, "") == []


# --- agent tools ---

def test_list_documents_tool(session):
    doc, _ = vault.add_document(
        session, filename="deed.txt", data=b"deed text", title="Deed", category="home"
    )
    rows = json.loads(execute_tool(session, "list_documents", {}))
    assert len(rows) == 1
    assert rows[0]["document_id"] == doc.id
    assert rows[0]["category"] == "home"
    assert "not yet annotated" in rows[0]["summary"]


def test_read_document_tool_pages(session):
    text = "A" * (READ_PAGE_CHARS + 500)
    doc, _ = vault.add_document(session, filename="long.txt", data=text.encode())
    page1 = json.loads(execute_tool(session, "read_document", {"document_id": doc.id}))
    assert page1["total_chars"] == len(text)
    assert len(page1["text"]) == READ_PAGE_CHARS
    assert page1["next_start_char"] == READ_PAGE_CHARS
    page2 = json.loads(execute_tool(
        session, "read_document",
        {"document_id": doc.id, "start_char": page1["next_start_char"]},
    ))
    assert len(page2["text"]) == 500
    assert "next_start_char" not in page2
    missing = json.loads(execute_tool(session, "read_document", {"document_id": "doc_nope"}))
    assert "error" in missing


def test_read_document_tool_flags_thin_text(session):
    doc, _ = vault.add_document(session, filename="scan.docx", data=b"not a zip")
    result = json.loads(execute_tool(session, "read_document", {"document_id": doc.id}))
    assert "no extractable text" in result["note"]


def test_search_documents_tool(session):
    vault.add_document(
        session, filename="note.txt", data=b"The note matures on 2054-05-01.",
        title="Mortgage note", category="home",
    )
    results = json.loads(execute_tool(session, "search_documents", {"query": "matures"}))
    assert results[0]["title"] == "Mortgage note"


def test_annotate_document_tool(session):
    doc, _ = vault.add_document(session, filename="will.txt", data=b"last will")
    r = json.loads(execute_tool(
        session, "annotate_document",
        {"document_id": doc.id, "summary": "Ford's will, executed 2024; executor = spouse."},
    ))
    assert r["annotated"]
    assert "executor" in session.get(Document, doc.id).summary
    rows = json.loads(execute_tool(session, "list_documents", {}))
    assert "executor" in rows[0]["summary"]
    missing = json.loads(execute_tool(
        session, "annotate_document", {"document_id": "doc_nope", "summary": "x"}
    ))
    assert "error" in missing
