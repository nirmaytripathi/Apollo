"""YouTube videos as research sources.

A pasted YouTube link is read before planning: read_video(topic) returns the video's transcript with
timestamps, which becomes source S1 for every section. Most chatbots only see a video's title and
description, then guess; Apollo uses the spoken words or stops with the reason.

1. Captions (youtube-transcript-api): the video's own subtitles, manual before auto-generated,
   English first. Other languages are translated to English by YouTube when it can (the claim
   verifier's embedding model is English-only), otherwise kept as they are.
2. If there are no captions, or YouTube refuses the request (it often blocks cloud servers):
   Gemini watches the video by URL and transcribes it (llm.transcribe_video). This text is model
   output, so it is labelled "gemini_transcript", never presented as the video's own captions. Gemini is shown
   only the first VIDEO_MAX_SECONDS (a whole long lecture timed out); a transcript that reaches that
   point is marked as cut.
3. If both fail, read_video raises VideoUnreadable with every reason. Nothing is ever summarised
   from the title alone.
Videos longer than VIDEO_MAX_SECONDS (35 min) are not read: the research form refuses the link (it learns the
length from the browser's YouTube player), and captions that show a longer video raise VideoTooLong.

Transcript lines look like "[04:12] what was said", one line per caption group, so a passage
picked by the writer or verifier can be traced back to the moment in the video (time_of).
"""

import math
import re

import requests

from core import config, replay

_ID = r"([A-Za-z0-9_-]{11})"
YOUTUBE_URL = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^\s#]*&)?v=|shorts/|live/|embed/)" + _ID + r"|youtu\.be/" + _ID + r")"
    r"[^\s]*"
)
_STAMP = re.compile(r"\[((?:\d+:)?\d{1,2}:\d{2})\]")
_STAMPED_LINE = re.compile(r"^\s*\[?((?:\d+:)?\d{1,2}:\d{2})\]?\s*[-–:]?\s*(.+)$")
_NOISE = re.compile(r"^\[[^\]]*\]$")  # caption-only cues such as "[Music]" or "[Applause]"
OEMBED_URL = "https://www.youtube.com/oembed"


class VideoUnreadable(Exception):
    """The video's words could not be read; the message says why, in plain language."""


class VideoTooLong(VideoUnreadable):
    """The video is longer than VIDEO_MAX_SECONDS (known from its captions)."""


def find_video(text: str) -> tuple[str, str] | None:
    """(video id, the link as written) for the first YouTube link in `text`, or None."""
    m = YOUTUBE_URL.search(text or "")
    return (m.group(1) or m.group(2), m.group(0)) if m else None


def watch_url(video_id: str, seconds: int | None = None) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" + (f"&t={seconds}s" if seconds else "")


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def seconds_of(stamp: str) -> int:
    total = 0
    for part in stamp.split(":"):
        total = total * 60 + int(part)
    return total


def time_of(content: str, passage: str) -> int | None:
    """Seconds into the video where `passage` (a sentence of the transcript) is spoken, or None."""
    probe = (passage or "").strip()[:80]
    at = content.find(probe) if probe else -1
    if at < 0:
        return None
    stamps = list(_STAMP.finditer(content[: at + 12]))  # +12: the passage may start with its own stamp
    return seconds_of(stamps[-1].group(1)) if stamps else None


def metadata(video_id: str, errors: list[str]) -> dict:
    """Title and channel from YouTube's oEmbed endpoint (no API key). Empty values if unavailable."""
    try:
        resp = requests.get(OEMBED_URL, params={"url": watch_url(video_id), "format": "json"},
                            timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        return {"title": data.get("title", ""), "channel": data.get("author_name", "")}
    except Exception as exc:
        errors.append(f"video metadata failed for {video_id}: {type(exc).__name__}: {str(exc)[:200]}")
        return {"title": "", "channel": ""}


def _lines(snippets: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Group short caption snippets into lines of about VIDEO_LINE_SECONDS, ending early at a sentence end."""
    out: list[tuple[float, str]] = []
    start, words = 0.0, []
    for at, text in snippets:
        text = " ".join(text.split())
        if not text or _NOISE.match(text):
            continue
        if not words:
            start = at
        words.append(text)
        joined = " ".join(words)
        if at - start >= config.VIDEO_LINE_SECONDS or (joined.endswith((".", "?", "!")) and len(joined) >= 60):
            out.append((start, joined))
            words = []
    if words:
        out.append((start, " ".join(words)))
    return out


def captions(video_id: str, errors: list[str]) -> tuple[list[tuple[float, str]], str]:
    """(snippets as (start seconds, text), language code) from the video's own captions; ([], "") if none."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        available = YouTubeTranscriptApi().list(video_id)
        try:
            chosen = available.find_transcript(config.VIDEO_LANGUAGES)
        except Exception:  # no caption in a preferred language: take the first one there is
            chosen = next(iter(available))
        if not chosen.language_code.startswith("en") and chosen.is_translatable:
            try:
                chosen = chosen.translate("en")
            except Exception as exc:
                errors.append(f"video captions for {video_id} kept in {chosen.language_code}: translation failed: {type(exc).__name__}")
        return [(s.start, s.text) for s in chosen.fetch()], chosen.language_code
    except Exception as exc:
        errors.append(f"video captions unavailable for {video_id}: {type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}")
        return [], ""


def parse_transcript(text: str) -> list[tuple[float, str]]:
    """Lines "[mm:ss] words" (as asked of Gemini) to (seconds, words). Unstamped lines join the previous one."""
    out: list[tuple[float, str]] = []
    for line in (text or "").splitlines():
        m = _STAMPED_LINE.match(line)
        if m:
            out.append((seconds_of(m.group(1)), m.group(2).strip()))
        elif line.strip() and out:
            out[-1] = (out[-1][0], f"{out[-1][1]} {line.strip()}")
    return out


def transcript_text(lines: list[tuple[float, str]]) -> tuple[str, float, bool]:
    """(text, seconds covered, truncated): lines "[mm:ss] words" up to VIDEO_MAX_CHARS."""
    kept, size = [], 0
    for at, words in lines:
        line = f"[{clock(at)}] {words}"
        if size + len(line) + 1 > config.VIDEO_MAX_CHARS:
            return "\n".join(kept), at, True
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept), (lines[-1][0] if lines else 0.0), False


def plan_excerpt(content: str, budget: int) -> str:
    """Lines spread evenly over the whole transcript (not just its start), within `budget` characters."""
    if len(content) <= budget:
        return content
    lines = content.split("\n")
    step = math.ceil(len(content) / budget)
    return "\n".join(lines[::step])


def read_video(topic: str, errors: list[str]) -> dict | None:
    """The YouTube video linked in `topic`, read; None when the topic has no YouTube link.

    Returns {id, url, link, title, channel, content, extracted_by, language, seconds, truncated}.
    Raises VideoUnreadable when neither captions nor Gemini give its words."""
    found = find_video(topic)
    if not found:
        return None
    if replay.active():
        raise VideoUnreadable("replay mode (LLM_MODE=replay) does not read videos")
    video_id, link = found
    meta = metadata(video_id, errors)
    reasons: list[str] = []

    snippets, language = captions(video_id, reasons)
    extracted_by = "youtube_captions"
    if snippets:
        if snippets[-1][0] > config.VIDEO_MAX_SECONDS + 30:  # the form refuses such links; this catches the rest
            title = f" \"{meta['title']}\"" if meta["title"] else ""
            raise VideoTooLong(
                f"The video{title} is about {round(snippets[-1][0] / 60)} minutes long. Apollo reads videos of up to "
                f"{config.VIDEO_MAX_SECONDS // 60} minutes; please use a shorter video or a part of it."
            )
        lines = _lines(snippets)
    else:
        lines, language, extracted_by = [], "en", "gemini_transcript"
        if not (config.VIDEO_GEMINI_FALLBACK and config.GEMINI_API_KEY):
            reasons.append("Gemini fallback is off or GEMINI_API_KEY is not set")
        else:
            from core.llm import transcribe_video

            # A model sometimes answers with no text, or is overloaded: try the next one before giving up.
            for model in config.VIDEO_GEMINI_MODELS:
                try:
                    raw = transcribe_video(watch_url(video_id), model)
                except Exception as exc:
                    reasons.append(f"Gemini {model} could not watch the video: {type(exc).__name__}: {str(exc)[:200]}")
                    continue
                lines = parse_transcript(raw)
                if lines:
                    break
                if "NO_SPEECH" in raw:
                    reasons.append(f"Gemini {model} found no speech in the video")
                    break  # a video without speech stays without speech on another model
                reasons.append(f"Gemini {model}'s transcript had no timestamped lines: {raw[:120]!r}")

    content, seconds, truncated = transcript_text(lines)
    if extracted_by == "gemini_transcript" and lines and lines[-1][0] >= config.VIDEO_MAX_SECONDS - 30:
        truncated = True  # Gemini was shown only the first VIDEO_MAX_SECONDS: the video goes on after that
    if len(content) < config.MIN_CONTENT_CHARS:
        errors.extend(reasons)
        title = f" \"{meta['title']}\"" if meta["title"] else ""
        raise VideoUnreadable(
            f"Could not read the video{title}: it has no usable captions and it could not be transcribed. "
            "Apollo does not guess a video's content from its title. Details: " + "; ".join(reasons or ["transcript too short"])
        )
    errors.extend(reasons)  # e.g. captions blocked, then Gemini succeeded: worth knowing, not fatal
    return {
        "id": video_id,
        "url": watch_url(video_id),
        "link": link,
        "title": meta["title"] or f"YouTube video {video_id}",
        "channel": meta["channel"],
        "content": content,
        "extracted_by": extracted_by,
        "language": language,
        "seconds": int(seconds),
        "truncated": truncated,
    }
