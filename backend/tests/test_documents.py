"""Attached documents (tools/documents.py): reading PDFs and text files, refusing what cannot be read,
and documents as cited sources in a run. Offline: small PDFs are built in memory; search and LLMs are fakes."""

import json

import pytest

import api
from core import config
from graph import nodes
from graph.state import ALL_SECTIONS
from graph.workflow import run_research
from tools import documents
from tools import passages as passages_mod
from verify import claims as claims_mod

from .fakes import PLAN, fake_embed, fake_llm, fake_search, verify_reply

COMPLETE = lambda n: '{"complete": true, "gaps": []}'


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one text line per page ("" = a page with no text, like a scanned image)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_id = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                       f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


PAGE1 = ("Walking thirty minutes a day lowered resting blood pressure in the trial group of older adults. "
         "The effect appeared after eight weeks and held for the rest of the study period.")
PAGE2 = ("Participants who walked daily also reported better sleep quality after twelve weeks of follow up. "
         "Sleep was measured with a standard questionnaire at the start and at the end.")


# ---------------------------------------------------------------- reading


def test_pdf_is_read_page_by_page_with_page_markers():
    doc = documents.read_document("study.pdf", make_pdf([PAGE1, PAGE2]))
    assert doc["kind"] == "pdf" and doc["pages"] == 2 and not doc["truncated"]
    assert doc["content"].split("\n") == [f"[p. 1] {PAGE1}", f"[p. 2] {PAGE2}"]
    assert documents.page_of(doc["content"], "Participants who walked daily also reported") == 2
    assert documents.page_of(doc["content"], "never in the file") is None


def test_scanned_pdf_without_text_is_refused_not_guessed():
    with pytest.raises(documents.DocumentUnreadable, match="no text layer"):
        documents.read_document("scan.pdf", make_pdf(["", ""]))


def test_broken_pdf_and_wrong_types_are_refused_with_the_reason(monkeypatch):
    with pytest.raises(documents.DocumentUnreadable, match="not a readable PDF"):
        documents.read_document("broken.pdf", b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(documents.DocumentUnreadable, match="PDF, .txt or .md"):
        documents.read_document("slides.pptx", b"anything")
    monkeypatch.setattr(config, "DOCUMENT_MAX_BYTES", 10)
    with pytest.raises(documents.DocumentUnreadable, match="larger than"):
        documents.read_document("notes.txt", b"x" * 11)


def test_text_files_keep_their_lines_and_long_ones_are_cut(monkeypatch):
    body = ("A long line of notes about walking and heart health, written by hand. " * 3 + "\n") * 10
    doc = documents.read_document("C:\\Users\\me\\notes.md", body.encode("utf-8"))
    assert doc["name"] == "notes.md" and doc["kind"] == "text" and doc["pages"] == 0  # no folder names kept
    monkeypatch.setattr(config, "DOCUMENT_MAX_CHARS", 500)
    cut = documents.read_document("notes.md", body.encode("utf-8"))
    assert cut["truncated"] and len(cut["content"]) <= 500


# ---------------------------------------------------------------- documents in a run


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. It reports a finding.", "trafilatura"))
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user))
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "m")])


def test_documents_are_sources_for_every_section_and_reach_the_planner(monkeypatch, offline):
    seen = {"plan": "", "writer": []}
    chat = fake_llm(COMPLETE, writer=lambda user: seen["writer"].append(user) or "The attached study reports lower blood pressure [S1].")

    def spy(system, user, **k):
        if system == nodes.PLAN_SYSTEM:
            seen["plan"] = user
        return chat(system, user, **k)

    monkeypatch.setattr(nodes, "llm_chat", spy)
    doc = documents.read_document("study.pdf", make_pdf([PAGE1, PAGE2]))
    final = run_research("walking and heart health", max_rounds=1, documents=[doc])

    assert "Attached documents" in seen["plan"] and "--- study.pdf" in seen["plan"] and "[p. 2]" in seen["plan"]
    s1 = final["findings"][0]
    assert s1["id"] == "S1" and s1["kind"] == "document" and s1["section"] == ALL_SECTIONS
    assert s1["url"] == "document://study.pdf" and s1["extracted_by"] == "pdf_text" and s1["pages"] == 2
    assert [f["id"] for f in final["findings"]] == [f"S{i}" for i in range(1, len(final["findings"]) + 1)]
    assert len(seen["writer"]) == len(PLAN) and all("[S1] (document the user attached) study.pdf" in u for u in seen["writer"])


def test_documents_follow_the_video_in_numbering(monkeypatch, offline):
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    monkeypatch.setattr(nodes, "read_video", lambda topic, errors: {
        "id": "x", "url": "https://www.youtube.com/watch?v=arj7oStGLkU", "link": "https://youtu.be/arj7oStGLkU",
        "title": "Talk", "channel": "", "content": "[00:01] Walking is good for the heart, the speaker says.",
        "extracted_by": "youtube_captions", "language": "en", "seconds": 1, "truncated": False})
    doc = documents.read_document("notes.txt", ("Walking notes. " * 30).encode())
    out = nodes.apollo_plan({"topic": "https://youtu.be/arj7oStGLkU", "events": [], "errors": [], "findings": [],
                             "documents": [doc]})
    assert [(f["id"], f["kind"]) for f in out["findings"]] == [("S1", "video"), ("S2", "document")]


# ---------------------------------------------------------------- API


@pytest.fixture
def client(monkeypatch, tmp_path, offline):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE, writer=lambda user: "Walking lowered resting blood pressure in the trial group [S1]."))
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'docs.db'}")
    monkeypatch.setattr(config, "LEGACY_USERS_DB", str(tmp_path / "none.db"))
    api._runs.clear()
    api._uploads.clear()
    c = TestClient(api.app)
    assert c.post("/api/auth/register", json={"name": "D", "email": "d@example.com", "password": "correct horse"}).status_code == 201
    return c


def _events(client, run_id):
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        return [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]


def test_upload_then_run_cites_the_document_with_page_numbers(client):
    up = client.post("/api/documents", files={"file": ("study.pdf", make_pdf([PAGE1, PAGE2]), "application/pdf")})
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["name"] == "study.pdf" and body["kind"] == "pdf" and body["pages"] == 2 and "content" not in body

    run = client.post("/api/runs", json={"topic": "walking and heart health", "max_rounds": 1, "documents": [body["id"]]})
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]
    events = _events(client, run_id)
    docs_event = next(e["data"] for e in events if e["type"] == "documents")
    assert docs_event["documents"] == [{"id": "S1", "name": "study.pdf", "pages": 2, "truncated": False}]

    report = client.get(f"/api/runs/{run_id}/report").json()
    s1 = next(s for s in report["sources"] if s["id"] == "S1")
    assert s1["kind"] == "document" and s1["pages"] == 2 and s1["url"] == "document://study.pdf"
    evidence = client.get(f"/api/runs/{run_id}/evidence").json()
    doc_passages = [p for c in evidence["claims"] for p in c["passages"] if p["source_id"] == "S1"]
    assert doc_passages and all(p["page"] in (1, 2) for p in doc_passages)


def test_unreadable_upload_is_rejected_with_the_reason(client):
    resp = client.post("/api/documents", files={"file": ("scan.pdf", make_pdf([""]), "application/pdf")})
    assert resp.status_code == 422 and "no text layer" in resp.json()["detail"]


def test_a_document_id_from_someone_else_or_expired_is_refused(client, monkeypatch):
    doc_id = client.post("/api/documents", files={"file": ("n.txt", ("Walking notes. " * 30).encode(), "text/plain")}).json()["id"]
    api._uploads[doc_id]["owner"] = "someone-else"
    resp = client.post("/api/runs", json={"topic": "walking", "max_rounds": 1, "documents": [doc_id]})
    assert resp.status_code == 400 and "attach it again" in resp.json()["detail"]
    resp = client.post("/api/runs", json={"topic": "walking", "max_rounds": 1, "documents": ["never-uploaded"]})
    assert resp.status_code == 400
