"""Word export (report.docx) and public read-only share links. Offline: search and LLMs are fakes."""

import io

import pytest
from docx import Document

import api
from core import config
from graph import nodes
from reports.docx_export import render_docx
from tools import passages as passages_mod
from verify import claims as claims_mod

from .fakes import fake_embed, fake_llm, fake_search, verify_reply
from .test_stop import COMPLETE, _wait


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. It reports a finding.", "trafilatura"))
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE))
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user))
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "m")])
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'share.db'}")
    monkeypatch.setattr(config, "LEGACY_USERS_DB", str(tmp_path / "none.db"))
    api._runs.clear()
    api._uploads.clear()
    return TestClient(api.app)


def _register(c, email):
    assert c.post("/api/auth/register", json={"name": "U", "email": email, "password": "correct horse"}).status_code == 201


def _finished_run(c):
    run_id = c.post("/api/runs", json={"topic": "walking and heart health", "max_rounds": 1}).json()["run_id"]
    assert _wait(c, run_id, lambda s: s["status"] not in api.ACTIVE)["status"] == "complete"
    return run_id


def _docx_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    cells = [cell.text for t in doc.tables for row in t.rows for cell in row.cells]
    return "\n".join([p.text for p in doc.paragraphs] + cells)


def test_render_docx_handles_markdown_and_bad_characters():
    state = {
        "topic": "t", "report": "# Title\n\n## Intro\n\nA **bold** and *italic* line [S1].\x01\n\n- point one\n1. step\n",
        "references": [{"id": "S1", "title": "Source one", "url": "https://example.com/a"}],
        "claims": [{"claim": "A line [S1].", "status": "verified", "source_ids": ["S1"]}],
    }
    text = _docx_text(render_docx(state))
    for part in ("Title", "Intro", "A bold and italic line [S1].", "point one", "step", "Source one", "verified", "Claim verification summary"):
        assert part in text
    assert "**" not in text and "\x01" not in text


def test_docx_download(client):
    _register(client, "a@example.com")
    run_id = _finished_run(client)
    resp = client.get(f"/api/runs/{run_id}/report.docx")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == api.DOCX_TYPE
    assert resp.headers["content-disposition"].endswith('.docx"')
    assert "References" in _docx_text(resp.content)


def test_share_link_is_public_read_only_and_can_be_turned_off(client):
    from fastapi.testclient import TestClient

    _register(client, "owner@example.com")
    run_id = _finished_run(client)
    assert client.get(f"/api/runs/{run_id}/share").json() == {"token": None}
    token = client.post(f"/api/runs/{run_id}/share").json()["token"]
    assert len(token) >= 40
    assert client.post(f"/api/runs/{run_id}/share").json()["token"] == token  # same link again

    stranger = TestClient(api.app)  # no sign-in
    shared = stranger.get(f"/api/shared/{token}")
    assert shared.status_code == 200
    body = shared.json()
    assert body["topic"] == "walking and heart health" and body["markdown"]
    assert "errors" not in body  # internal details stay private
    assert stranger.get(f"/api/shared/{token}/report.docx").status_code == 200
    assert stranger.get(f"/api/shared/{token}/report.md").status_code == 200
    # The token opens only the report: the run itself and its actions stay private.
    assert stranger.get(f"/api/runs/{run_id}/report").status_code == 401
    assert stranger.post(f"/api/runs/{run_id}/share").status_code == 401

    other = TestClient(api.app)
    _register(other, "other@example.com")
    assert other.post(f"/api/runs/{run_id}/share").status_code == 404  # only the owner can share
    assert other.delete(f"/api/runs/{run_id}/share").status_code == 404

    assert client.delete(f"/api/runs/{run_id}/share").json() == {"ok": True}
    assert stranger.get(f"/api/shared/{token}").status_code == 404
    assert stranger.get("/api/shared/not-a-token").status_code == 404


def test_share_survives_a_save_of_the_run(client):
    """The run's own saves never write share_token, so they cannot turn a link off."""
    _register(client, "owner@example.com")
    run_id = _finished_run(client)
    token = client.post(f"/api/runs/{run_id}/share").json()["token"]
    api._save(api._runs[run_id])
    assert client.get(f"/api/shared/{token}").status_code == 200


def test_share_needs_a_finished_report(client, monkeypatch):
    _register(client, "owner@example.com")
    run_id = client.post("/api/runs", json={"topic": "walking and heart health", "max_rounds": 1, "hitl": True}).json()["run_id"]
    _wait(client, run_id, lambda s: s["status"] == "awaiting_input")
    assert client.post(f"/api/runs/{run_id}/share").status_code == 409
    client.post(f"/api/runs/{run_id}/stop")
