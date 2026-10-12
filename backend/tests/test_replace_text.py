from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
import pymupdf as fitz

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    return TestClient(main.app)


def make_pdf_bytes(names=("Arnav Agarwal", "Arnav Agarwal")) -> bytes:
    doc = fitz.open()
    for name in names:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 120), name, fontsize=12, fontname="helv")
        page.insert_text((72, 140), "Some other paragraph stays intact.", fontsize=11, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def upload(client: TestClient, data: bytes | None = None) -> str:
    data = data or make_pdf_bytes()
    r = client.post("/api/documents", files={"file": ("test.pdf", io.BytesIO(data), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def block_for(client: TestClient, did: str, page: int, needle: str) -> dict:
    r = client.get(f"/api/documents/{did}/pages/{page}/blocks")
    assert r.status_code == 200, r.text
    for b in r.json()["blocks"]:
        if needle in b["text"]:
            return b
    raise AssertionError(f"block containing {needle!r} not found on page {page}")


def replace(client: TestClient, did: str, page: int, rect, text: str):
    return client.post(
        f"/api/documents/{did}/replace-text",
        json={"page_number": page, "rect": list(rect), "text": text},
    )


def page_text(client: TestClient, did: str, page: int) -> str:
    r = client.get(f"/api/documents/{did}/pages/{page}/blocks")
    assert r.status_code == 200, r.text
    return "\n".join(b["text"] for b in r.json()["blocks"])


def export_bytes(client: TestClient, did: str) -> bytes:
    r = client.get(f"/api/documents/{did}/export")
    assert r.status_code == 200, r.text
    return r.content


def exported_text(client: TestClient, did: str, page: int = 0) -> str:
    with fitz.open(stream=export_bytes(client, did), filetype="pdf") as doc:
        return doc[page].get_text()


def test_replace_name_is_verified(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    r = replace(client, did, 0, b["rect"], "Pratibha Yadav")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replaced"] is True and body["verified"] is True
    assert "Pratibha Yadav" in page_text(client, did, 0)


def test_replace_short_with_longer_name(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    r = replace(client, did, 0, b["rect"], "Pratibha Kumari Yadav")
    assert r.status_code == 200, r.text
    assert "Pratibha Kumari Yadav" in page_text(client, did, 0)


def test_replace_text_with_spaces(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    r = replace(client, did, 0, b["rect"], "Dr. Pratibha Yadav")
    assert r.status_code == 200, r.text
    assert "Dr. Pratibha Yadav" in page_text(client, did, 0)


def test_replace_multiline_content(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    r = replace(client, did, 0, b["rect"], "Pratibha Yadav\nSenior Engineer")
    assert r.status_code == 200, r.text
    text = page_text(client, did, 0)
    assert "Pratibha Yadav" in text and "Senior Engineer" in text


def test_replace_on_non_first_page(client):
    did = upload(client)
    b = block_for(client, did, 1, "Arnav Agarwal")
    r = replace(client, did, 1, b["rect"], "Pratibha Yadav")
    assert r.status_code == 200, r.text
    assert "Pratibha Yadav" in page_text(client, did, 1)
    assert "Arnav Agarwal" in page_text(client, did, 0)


def test_replacement_present_in_extracted_text(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    replace(client, did, 0, b["rect"], "Pratibha Yadav")
    extracted = page_text(client, did, 0)
    assert "Pratibha Yadav" in extracted


def test_export_contains_replacement(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    replace(client, did, 0, b["rect"], "Pratibha Yadav")
    text = exported_text(client, did, 0)
    assert "Pratibha Yadav" in text
    assert "Arnav Agarwal" not in text


def test_original_removed_from_region(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    replace(client, did, 0, b["rect"], "Pratibha Yadav")
    text = page_text(client, did, 0)
    assert "Arnav Agarwal" not in text
    assert "Some other paragraph stays intact." in text


def test_unfittable_replacement_does_not_corrupt(client, monkeypatch):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    monkeypatch.setattr(main, "insert_fitted", lambda *a, **k: (None, None, []))
    r = replace(client, did, 0, b["rect"], "Pratibha Yadav")
    assert r.status_code == 422, r.text
    text = page_text(client, did, 0)
    assert "Arnav Agarwal" in text
    assert "Some other paragraph stays intact." in text


def test_verification_failure_does_not_corrupt(client, monkeypatch):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    monkeypatch.setattr(main, "verify_signature", lambda *a, **k: False)
    r = replace(client, did, 0, b["rect"], "Pratibha Yadav")
    assert r.status_code == 422, r.text
    text = page_text(client, did, 0)
    assert "Arnav Agarwal" in text
    assert "Some other paragraph stays intact." in text


def test_very_long_replacement_still_fits_without_discarding(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    long_text = " ".join(["Pratibha"] * 60)
    r = replace(client, did, 0, b["rect"], long_text)
    assert r.status_code == 200, r.text
    assert "Pratibha" in page_text(client, did, 0)


def test_preview_changes_after_edit(client):
    did = upload(client)
    before = client.get(f"/api/documents/{did}/pages/0/preview").content
    b = block_for(client, did, 0, "Arnav Agarwal")
    replace(client, did, 0, b["rect"], "Pratibha Yadav")
    after = client.get(f"/api/documents/{did}/pages/0/preview").content
    assert before != after


def test_repeated_edits(client):
    did = upload(client)
    b = block_for(client, did, 0, "Arnav Agarwal")
    replace(client, did, 0, b["rect"], "Pratibha Yadav")
    b2 = block_for(client, did, 0, "Pratibha Yadav")
    r = replace(client, did, 0, b2["rect"], "Arnav Agarwal")
    assert r.status_code == 200, r.text
    text = page_text(client, did, 0)
    assert "Arnav Agarwal" in text and "Pratibha Yadav" not in text
