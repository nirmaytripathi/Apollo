"""YouTube links (tools/video.py): reading the transcript, the fallbacks, and the video as source S1.

Offline: captions, oEmbed metadata and Gemini are replaced by fakes, so these tests check our rules
(what counts as readable, never guessing from the title, timestamps), not YouTube or Gemini.
"""

import json
import time

import pytest

import api
from core import config, llm
from graph import nodes
from graph.state import ALL_SECTIONS
from graph.workflow import run_research
from tools import passages as passages_mod
from tools import video
from verify import claims as claims_mod

from .fakes import PLAN, fake_embed, fake_llm, fake_search, verify_reply

COMPLETE = lambda n: '{"complete": true, "gaps": []}'
LINK = "https://youtu.be/arj7oStGLkU?si=share"
SPOKEN = [
    (0.0, "[Music]"),
    (2.0, "intermittent fasting"),
    (5.5, "lowers blood pressure in most adults over a few weeks."),
    (9.0, "Most people lose weight"),
    (14.0, "within twelve weeks of starting"),
    (31.0, "but sleep matters more than diet."),
]


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize("text,vid", [
    ("check https://www.youtube.com/watch?v=arj7oStGLkU please", "arj7oStGLkU"),
    ("https://youtube.com/watch?feature=share&v=arj7oStGLkU&t=30", "arj7oStGLkU"),
    ("https://m.youtube.com/shorts/arj7oStGLkU", "arj7oStGLkU"),
    ("youtu.be/arj7oStGLkU", "arj7oStGLkU"),
    ("https://www.youtube.com/live/arj7oStGLkU?si=x", "arj7oStGLkU"),
])
def test_find_video_accepts_every_link_form(text, vid):
    assert video.find_video(text)[0] == vid


def test_find_video_ignores_other_links_and_plain_topics():
    assert video.find_video("drones in farming") is None
    assert video.find_video("https://vimeo.com/123456789") is None
    assert video.find_video("https://www.youtube.com/@TED") is None  # a channel, not a video


def test_caption_snippets_become_timed_lines_without_noise():
    lines = video._lines(SPOKEN)
    assert lines[0] == (2.0, "intermittent fasting lowers blood pressure in most adults over a few weeks.")  # ends at the sentence
    assert all("[Music]" not in text for _, text in lines)
    assert lines[1][0] == 9.0 and lines[-1][1].endswith("more than diet.")


def test_transcript_text_stamps_lines_and_marks_a_cut(monkeypatch):
    lines = video._lines(SPOKEN)
    text, seconds, truncated = video.transcript_text(lines)
    assert text.startswith("[00:02] intermittent fasting") and not truncated
    monkeypatch.setattr(config, "VIDEO_MAX_CHARS", 100)
    text, seconds, truncated = video.transcript_text(lines)
    assert truncated and text.count("\n") == 0 and seconds == 9.0  # read up to where it was cut


def test_parse_transcript_reads_stamps_and_joins_unstamped_lines():
    raw = "[00:04] First sentence.\nstill the first one\n[1:02:05] Late sentence.\nNO_SPEECH?"
    assert video.parse_transcript(raw) == [(4, "First sentence. still the first one"), (3725, "Late sentence. NO_SPEECH?")]
    assert video.clock(3725) == "1:02:05" and video.clock(75) == "01:15"


def test_time_of_finds_the_moment_a_passage_is_said():
    content = "[00:02] Fasting lowers blood pressure. Most adults lose weight.\n[01:15] Sleep matters more."
    assert video.time_of(content, "Most adults lose weight.") == 2  # second sentence of the line
    assert video.time_of(content, "[01:15] Sleep matters more.") == 75
    assert video.time_of(content, "Never said in the video.") is None


def test_plan_excerpt_samples_the_whole_video_not_just_its_start():
    content = "\n".join(f"[{i:02d}:00] line {i} " + "x" * 40 for i in range(60))
    excerpt = video.plan_excerpt(content, 1000)
    assert len(excerpt) <= 1100 and "line 0 " in excerpt and "line 5" in excerpt.split("\n")[-1]


# ---------------------------------------------------------------- read_video


@pytest.fixture
def meta(monkeypatch):
    monkeypatch.setattr(video, "metadata", lambda vid, errors: {"title": "Fasting facts", "channel": "Health Talk"})


def test_read_video_uses_the_captions(monkeypatch, meta):
    monkeypatch.setattr(video, "captions", lambda vid, errors: (SPOKEN * 3, "en"))
    errors = []
    v = video.read_video(f"is this true? {LINK}", errors)
    assert v["id"] == "arj7oStGLkU" and v["url"] == "https://www.youtube.com/watch?v=arj7oStGLkU" and v["link"] == LINK
    assert v["extracted_by"] == "youtube_captions" and v["title"] == "Fasting facts" and v["channel"] == "Health Talk"
    assert v["content"].startswith("[00:02] ") and errors == []


def test_read_video_falls_back_to_gemini_when_captions_fail(monkeypatch, meta):
    def blocked(vid, errors):
        errors.append("video captions unavailable for x: RequestBlocked")
        return [], ""

    monkeypatch.setattr(video, "captions", blocked)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    said = "\n".join(f"[00:{i:02d}] Spoken sentence number {i} about fasting and health." for i in range(10))
    monkeypatch.setattr(llm, "transcribe_video", lambda url, model=None: said)
    errors = []
    v = video.read_video(LINK, errors)
    assert v["extracted_by"] == "gemini_transcript" and "[00:09] Spoken sentence number 9" in v["content"]
    assert any("RequestBlocked" in e for e in errors)  # the blocked captions are still reported


def test_gemini_sees_only_the_start_of_a_long_video_and_the_cut_is_marked(monkeypatch, meta):
    """Seen live: a whole 2-hour lecture made Gemini time out (504). Only the first VIDEO_MAX_SECONDS are sent."""
    from google.genai import types

    sent = {}

    class Models:
        def generate_content(self, model, contents, config):
            sent["clip"] = contents.parts[0].video_metadata
            text = "\n".join(f"[{m:02d}:00] The lecture goes on with point number {m} about functions." for m in range(21))
            return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[types.Part(text=text)]))])

    class Client:
        models = Models()

    monkeypatch.setattr(video, "captions", lambda vid, errors: ([], ""))
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "VIDEO_MAX_SECONDS", 1200)
    monkeypatch.setattr(llm, "_get_client", lambda provider: Client())
    v = video.read_video(LINK, [])
    assert sent["clip"].start_offset == "0s" and sent["clip"].end_offset == "1200s" and sent["clip"].fps == config.VIDEO_GEMINI_FPS
    assert v["extracted_by"] == "gemini_transcript" and v["truncated"] is True and v["seconds"] == 1200


def test_captions_of_a_video_over_the_limit_stop_the_run_with_the_reason(monkeypatch, meta):
    long_talk = [(float(s), f"sentence spoken at second {s}.") for s in range(0, 2400, 10)]  # 40 minutes
    monkeypatch.setattr(video, "captions", lambda vid, errors: (long_talk, "en"))
    monkeypatch.setattr(config, "VIDEO_MAX_SECONDS", 2100)
    with pytest.raises(video.VideoTooLong, match=r"about 40 minutes long.*up to 35 minutes"):
        video.read_video(LINK, [])
    assert issubclass(video.VideoTooLong, video.VideoUnreadable)  # the API handles it like any unreadable video


def test_captions_of_a_video_within_the_limit_are_read(monkeypatch, meta):
    talk = [(float(s), f"sentence spoken at second {s}.") for s in range(0, 2100, 10)]  # 35 minutes
    monkeypatch.setattr(video, "captions", lambda vid, errors: (talk, "en"))
    monkeypatch.setattr(config, "VIDEO_MAX_SECONDS", 2100)
    assert video.read_video(LINK, [])["extracted_by"] == "youtube_captions"


def test_an_empty_or_overloaded_gemini_model_hands_the_video_to_the_next(monkeypatch, meta):
    """Seen live: a model answered with no text, another with 503; the next model transcribed the video."""
    monkeypatch.setattr(video, "captions", lambda vid, errors: ([], ""))
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "VIDEO_GEMINI_MODELS", ["empty-model", "busy-model", "good-model"])
    said = "\n".join(f"[00:{i:02d}] Spoken sentence number {i} about fasting and health." for i in range(10))
    tried = []

    def transcribe(url, model=None):
        tried.append(model)
        if model == "busy-model":
            raise RuntimeError("503 UNAVAILABLE")
        return "" if model == "empty-model" else said

    monkeypatch.setattr(llm, "transcribe_video", transcribe)
    errors = []
    v = video.read_video(LINK, errors)
    assert tried == ["empty-model", "busy-model", "good-model"] and v["extracted_by"] == "gemini_transcript"
    assert any("empty-model's transcript had no timestamped lines" in e for e in errors)
    assert any("busy-model could not watch" in e for e in errors)


def test_unreadable_video_stops_with_the_reasons_and_never_guesses(monkeypatch, meta):
    monkeypatch.setattr(video, "captions", lambda vid, errors: ([], ""))
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    tried = []
    monkeypatch.setattr(llm, "transcribe_video", lambda url, model=None: tried.append(model) or "NO_SPEECH")
    with pytest.raises(video.VideoUnreadable) as exc:
        video.read_video(LINK, [])
    assert '"Fasting facts"' in str(exc.value) and "does not guess" in str(exc.value)
    assert "no speech" in str(exc.value)
    assert len(tried) == 1  # no speech is not a model failure: the other models are not asked


def test_unreadable_without_gemini_key_says_so(monkeypatch, meta):
    monkeypatch.setattr(video, "captions", lambda vid, errors: ([], ""))
    with pytest.raises(video.VideoUnreadable, match="GEMINI_API_KEY is not set"):
        video.read_video(LINK, [])


def test_topic_without_a_link_is_not_a_video():
    assert video.read_video("drones in farming", []) is None


# ---------------------------------------------------------------- the video in a run

TRANSCRIPT = ("[00:05] Intermittent fasting lowers blood pressure in most adults within weeks.\n"
              "[00:40] The speaker says weight loss comes from eating less overall.\n"
              "[01:20] Sleep matters more than diet for long term health.")
VIDEO = {"id": "arj7oStGLkU", "url": "https://www.youtube.com/watch?v=arj7oStGLkU", "link": LINK,
         "title": "Fasting facts", "channel": "Health Talk", "content": TRANSCRIPT,
         "extracted_by": "youtube_captions", "language": "en", "seconds": 80, "truncated": False}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. It reports a finding.", "trafilatura"))
    monkeypatch.setattr(nodes, "read_video", lambda topic, errors: dict(VIDEO))
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user))
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "m")])


def test_video_is_source_s1_for_every_section_and_steers_the_plan(monkeypatch, offline):
    seen = {"plan": [], "writer": []}
    chat = fake_llm(COMPLETE, writer=lambda user: seen["writer"].append(user) or "The video says fasting lowers blood pressure [S1].")

    def spy(system, user, **k):
        if system in (nodes.PLAN_SYSTEM, nodes.VIDEO_PLAN_SYSTEM):
            seen["plan"].append((system, user))
        return chat(system, user, **k)

    monkeypatch.setattr(nodes, "llm_chat", spy)
    final = run_research(f"{LINK} is this right?", max_rounds=1)

    system, user = seen["plan"][0]
    assert system == nodes.VIDEO_PLAN_SYSTEM and "User's question: is this right?" in user
    assert "Video: Fasting facts by Health Talk" in user and "[00:40]" in user and LINK not in user
    s1 = final["findings"][0]
    assert s1["id"] == "S1" and s1["kind"] == "video" and s1["section"] == ALL_SECTIONS and s1["content"] == TRANSCRIPT
    assert [f["id"] for f in final["findings"]] == [f"S{i}" for i in range(1, len(final["findings"]) + 1)]  # no gap, no clash
    assert len(seen["writer"]) == len(PLAN) and all("[S1] (video transcript) Fasting facts" in u for u in seen["writer"])
    assert final["video"]["title"] == "Fasting facts" and "content" not in final["video"]
    assert final["report"].startswith("# Fact check: Fasting facts\n")  # titled by the video, not its link


def test_writer_is_told_a_transcript_is_a_claim_not_evidence():
    assert "(video transcript)" in nodes.WRITE_SYSTEM and "not whether it is true" in nodes.WRITE_SYSTEM


@pytest.fixture
def client(monkeypatch, tmp_path, offline):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(nodes, "llm_chat", fake_llm(COMPLETE, writer=lambda user: "The video says fasting lowers blood pressure in most adults [S1]."))
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'apollo-test.db'}")
    monkeypatch.setattr(config, "LEGACY_USERS_DB", str(tmp_path / "no-legacy-users.db"))
    api._runs.clear()
    c = TestClient(api.app)
    assert c.post("/api/auth/register", json={"name": "T", "email": "t@example.com", "password": "correct horse"}).status_code == 201
    return c


def _events(client, run_id):
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        return [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]


def _start(client, topic):
    resp = client.post("/api/runs", json={"topic": topic, "max_rounds": 1})
    assert resp.status_code == 200, resp.text
    return resp.json()["run_id"]


def test_api_announces_the_video_and_links_evidence_to_its_moment(client):
    run_id = _start(client, LINK)
    events = _events(client, run_id)
    assert [e["type"] for e in events][:2] == ["video", "plan"]
    assert events[0]["data"]["title"] == "Fasting facts" and events[0]["data"]["extracted_by"] == "youtube_captions"
    # S1 came from planning, so the first search reports only the new web sources
    first_done = next(e["data"] for e in events if e["type"] == "search_done")
    assert all(s["id"] != "S1" for s in first_done["sources"])

    report = client.get(f"/api/runs/{run_id}/report").json()
    assert report["video"]["id"] == "arj7oStGLkU"
    s1 = next(s for s in report["sources"] if s["id"] == "S1")
    assert s1["kind"] == "video" and s1["section"] == 0 and s1["question"] == ""

    evidence = client.get(f"/api/runs/{run_id}/evidence").json()
    video_passages = [p for c in evidence["claims"] for p in c["passages"] if p["source_id"] == "S1"]
    assert video_passages and all(p["start"] in (5, 40, 80) for p in video_passages)
    web = [p for c in evidence["claims"] for p in c["passages"] if p["source_id"] != "S1"]
    assert all("start" not in p for p in web)


def test_api_reports_an_unreadable_video_as_a_clear_failure(client, monkeypatch):
    def unreadable(topic, errors):
        raise video.VideoUnreadable("Could not read the video \"Fasting facts\": it has no usable captions.")

    monkeypatch.setattr(nodes, "read_video", unreadable)
    run_id = _start(client, LINK)
    events = _events(client, run_id)
    assert [e["type"] for e in events] == ["error", "complete"]
    assert events[0]["data"]["message"].startswith("Could not read the video")  # no "Run failed: TypeName"
    assert events[1]["data"] == {"status": "failed", "stop_reason": "video_unreadable"}
