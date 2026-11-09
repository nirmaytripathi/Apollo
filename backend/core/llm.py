"""Single LLM client wrapper used by every agent (planning, review, writing, verification).

The default provider is LLM_PROVIDER in .env ("groq" or "gemini"). Callers
use `chat(system, user, temperature)` and get plain text back; JSON parsing
is done by the caller with tools.jsonparse.parse_json.

Each call has an HTTP timeout and a bounded number of retries on transient errors (tenacity), so a
slow or failing provider raises an exception instead of hanging. Callers are
expected to catch that exception and fall back.

Without `provider` / `model`, a call walks config.LLM_CHAIN (LLM_PROVIDER's model first, then
the others), so one model's spent quota or rate-limit burst does not fail planning, review or
writing. `provider` / `model` pin one model for a call; claim verification uses this to walk
its own chain (Groq 20b -> Groq 120b -> Gemini), because each has its own quota. gpt-oss models are called with reasoning_effort="low": their hidden
reasoning tokens count against Groq's token limits.
"""

import time
from typing import Callable

from core import config, live, metering, replay
from tools.retry_policy import ConfigError, is_quota_exhausted, with_retries

_clients: dict[str, object] = {}


def _get_client(provider: str):
    """Create each provider's client lazily so importing this module never needs keys."""
    if provider in _clients:
        return _clients[provider]
    if provider == "groq":
        if not config.GROQ_API_KEY:
            raise ConfigError("GROQ_API_KEY is not set")
        from groq import Groq

        client = Groq(api_key=config.GROQ_API_KEY, timeout=config.REQUEST_TIMEOUT_SECONDS * 2, max_retries=0)
    elif provider == "gemini":
        if not config.GEMINI_API_KEY:
            raise ConfigError("GEMINI_API_KEY is not set")
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=config.REQUEST_TIMEOUT_SECONDS * 2 * 1000),
        )
    else:
        raise ConfigError(f"Unknown LLM provider: {provider!r}")
    _clients[provider] = client
    return client


_cooling: dict[tuple[str, str], float] = {}  # (provider, model) -> monotonic time its quota may be back
_KEYS = {"groq": lambda: config.GROQ_API_KEY, "gemini": lambda: config.GEMINI_API_KEY}

QUOTA_MESSAGE = (
    "Every AI model's free quota is used up right now (Groq and Gemini daily limits), so Apollo stopped "
    "before searching instead of returning an empty report. Each model is tried again {when}; a new or "
    "paid API key in .env also fixes it."
)


class LLMUnavailable(Exception):
    """Every model in LLM_CHAIN has run out of quota: a run cannot write its report."""


def all_exhausted() -> bool:
    """True when every keyed model in LLM_CHAIN is skipped for a spent quota (known from earlier calls)."""
    now = time.monotonic()
    keyed = [(p, m) for p, m in dict.fromkeys(config.LLM_CHAIN) if _KEYS.get(p, lambda: "")()]
    return bool(keyed) and all(_cooling.get(c, 0) > now for c in keyed)


def quota_message() -> str:
    now = time.monotonic()
    waits = [t - now for t in _cooling.values() if t > now]
    when = f"in about {max(1, round(min(waits) / 60))} min" if waits else "shortly"
    return QUOTA_MESSAGE.format(when=when)


def chat(
    system: str, user: str, temperature: float = 0.2, model: str | None = None, provider: str | None = None,
    on_text: Callable[[str], None] | None = None,
) -> str:
    """Send one system+user prompt and return the model's text reply.

    With no provider/model given, walk config.LLM_CHAIN: a model whose call still fails after its
    retries (spent quota, rate-limit burst, outage) hands the call to the next model. A model whose
    DAILY quota is spent is skipped for LLM_COOLDOWN_SECONDS. With an explicit provider/model (claim
    verification runs its own chain) exactly that model is used.

    on_text (optional) streams the reply: it is called with the text so far as it arrives. If a model
    fails and the next one takes over, the text restarts from that model's first chunk."""
    live.raise_if_stopped()  # the user pressed Pause: no further model calls for this run
    if replay.active():  # LLM_MODE=replay: a fixed offline reply, no provider is called
        return replay.chat(system, user, on_text)
    if provider or model:
        provider = provider or config.LLM_PROVIDER
        return _chat_one(provider, model or (config.GROQ_MODEL if provider == "groq" else config.GEMINI_MODEL), system, user, temperature, on_text)

    now = time.monotonic()
    keyed = [(p, m) for p, m in dict.fromkeys(config.LLM_CHAIN) if _KEYS.get(p, lambda: "")()]
    # No key anywhere: still try the first entry, so the caller gets that provider's own ConfigError.
    candidates = [c for c in keyed if _cooling.get(c, 0) <= now] or keyed[:1] or config.LLM_CHAIN[:1]
    last: Exception | None = None
    for p, m in candidates:
        try:
            return _chat_one(p, m, system, user, temperature, on_text)
        except live.RunStopped:
            raise  # paused during a retry wait: not a model failure, so no next model is tried
        except Exception as exc:
            if is_quota_exhausted(exc):
                _cooling[(p, m)] = time.monotonic() + config.LLM_COOLDOWN_SECONDS
            last = exc  # try the next model; the meter already recorded this failure
    raise last if last else ConfigError("LLM_CHAIN is empty")


@with_retries(config.LLM_MAX_RETRIES)
def _chat_one(provider: str, name: str, system: str, user: str, temperature: float,
              on_text: Callable[[str], None] | None = None) -> str:
    """One model, with retries on transient errors. Every attempt (including failed ones) is
    recorded in the usage meter, with token counts as reported by the provider."""
    started = time.monotonic()
    try:
        if on_text is None:
            text, prompt_tokens, completion_tokens = _call(provider, name, system, user, temperature)
        else:
            text, prompt_tokens, completion_tokens = _stream(provider, name, system, user, temperature, on_text)
    except Exception as exc:
        metering.record("llm", False, time.monotonic() - started, provider=provider, model=name,
                        error=f"{type(exc).__name__}: {str(exc)[:400]}")
        raise
    metering.record("llm", True, time.monotonic() - started, provider=provider, model=name,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return text


TRANSCRIBE_PROMPT = (
    "Transcribe the speech in this video. One line per sentence, each starting with the time it is "
    "spoken as [mm:ss] (or [h:mm:ss]). Keep the speaker's words; if the speech is not in English, "
    "translate each line to English. Do not summarise, add or explain anything. If the video has no "
    "speech, reply NO_SPEECH. Output only the transcript."
)


@with_retries(config.LLM_MAX_RETRIES)
def transcribe_video(url: str, model: str | None = None) -> str:
    """Gemini watches a public YouTube video by URL and returns "[mm:ss] sentence" lines (tools/video.py).
    Low media resolution: the words are what matters, and it keeps a 15-minute video near 50k tokens.
    Only the first VIDEO_MAX_SECONDS are sent, at VIDEO_GEMINI_FPS frames a second: a long lecture
    sent whole made Gemini time out (504 DEADLINE_EXCEEDED), and only about that much transcript is kept anyway.
    A reply with no text returns "" (tools/video.py then tries the next model in VIDEO_GEMINI_MODELS)."""
    if replay.active():
        raise ConfigError("replay mode does not transcribe videos")
    from google.genai import types

    name, started = model or config.VIDEO_GEMINI_MODEL, time.monotonic()
    clip = types.VideoMetadata(start_offset="0s", end_offset=f"{config.VIDEO_MAX_SECONDS}s", fps=config.VIDEO_GEMINI_FPS)
    try:
        resp = _get_client("gemini").models.generate_content(
            model=name,
            contents=types.Content(parts=[types.Part(file_data=types.FileData(file_uri=url), video_metadata=clip),
                                          types.Part(text=TRANSCRIBE_PROMPT)]),
            config=types.GenerateContentConfig(
                temperature=0,
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                http_options=types.HttpOptions(timeout=config.VIDEO_TIMEOUT_SECONDS * 1000),
            ),
        )
    except Exception as exc:
        metering.record("llm", False, time.monotonic() - started, provider="gemini", model=name,
                        error=f"{type(exc).__name__}: {str(exc)[:400]}")
        raise
    usage = getattr(resp, "usage_metadata", None)
    metering.record("llm", True, time.monotonic() - started, provider="gemini", model=name,
                    prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    completion_tokens=(getattr(usage, "candidates_token_count", 0) or 0)
                    + (getattr(usage, "thoughts_token_count", 0) or 0))
    return resp.text or ""


def _call(provider: str, name: str, system: str, user: str, temperature: float) -> tuple[str, int, int]:
    """One provider call: (reply text, prompt tokens, completion tokens)."""
    client = _get_client(provider)
    if provider == "groq":
        extra = {"reasoning_effort": "low"} if name.startswith("openai/gpt-oss") else {}
        resp = client.chat.completions.create(
            model=name,
            temperature=temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **extra,
        )
        usage = getattr(resp, "usage", None)
        return (resp.choices[0].message.content or "",
                getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)

    from google.genai import types

    resp = client.models.generate_content(
        model=name,
        contents=user,
        config=types.GenerateContentConfig(system_instruction=system, temperature=temperature),
    )
    usage = getattr(resp, "usage_metadata", None)
    # Gemini bills thinking tokens as output, so they are counted with the completion.
    completion = (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
    return resp.text or "", getattr(usage, "prompt_token_count", 0) or 0, completion


def _stream(provider: str, name: str, system: str, user: str, temperature: float,
            on_text: Callable[[str], None]) -> tuple[str, int, int]:
    """Like _call, but streams: on_text(text so far) after each chunk. Token usage comes from the
    final chunk (Groq: x_groq.usage; Gemini: usage_metadata), 0 if the provider omits it."""
    client = _get_client(provider)
    text, prompt_tokens, completion_tokens = "", 0, 0
    if provider == "groq":
        extra = {"reasoning_effort": "low"} if name.startswith("openai/gpt-oss") else {}
        for chunk in client.chat.completions.create(
            model=name,
            temperature=temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            stream=True,
            **extra,
        ):
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                text += delta
                on_text(text)
            usage = getattr(getattr(chunk, "x_groq", None), "usage", None) or getattr(chunk, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or prompt_tokens
                completion_tokens = getattr(usage, "completion_tokens", 0) or completion_tokens
        return text, prompt_tokens, completion_tokens

    from google.genai import types

    for chunk in client.models.generate_content_stream(
        model=name,
        contents=user,
        config=types.GenerateContentConfig(system_instruction=system, temperature=temperature),
    ):
        if chunk.text:
            text += chunk.text
            on_text(text)
        usage = getattr(chunk, "usage_metadata", None)
        if usage:
            prompt_tokens = getattr(usage, "prompt_token_count", 0) or prompt_tokens
            completion_tokens = ((getattr(usage, "candidates_token_count", 0) or 0)
                                 + (getattr(usage, "thoughts_token_count", 0) or 0)) or completion_tokens
    return text, prompt_tokens, completion_tokens
