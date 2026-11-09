"""Test-wide safety net: tests must never call real APIs.

Every API key is blanked for each test, so any call that is not replaced by a
fake fails at once with ConfigError instead of spending quota (or waiting on
rate limits). The per-provider LLM client cache is cleared too, in case a client was built earlier.
"""

import pytest

from core import config, llm
from tools import passages


@pytest.fixture(autouse=True)
def no_real_api_keys(monkeypatch, tmp_path):
    for name in ("TAVILY_API_KEY", "SERPER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(config, name, "")
    # The specialist source APIs need no key, so they are switched off unless a test turns them on, and every
    # test gets its own empty source cache. Replay mode is off unless a test asks for it.
    monkeypatch.setattr(config, "DOMAIN_SOURCES", ())
    monkeypatch.setattr(config, "SOURCE_CACHE_DB", str(tmp_path / "source_cache.db"))
    monkeypatch.setattr(config, "LLM_MODE", "live")
    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.setattr(llm, "_cooling", {})  # a model marked out of quota must not leak between tests
    monkeypatch.setattr(config, "FETCH_PUBLISH_DATES", False)  # no page downloads for dates
    passages.clear_cache()  # embeddings cached by one test (with its fake model) must not leak into the next
