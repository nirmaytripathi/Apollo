"""Documents the user attaches (PDF, .txt, .md) as research sources.

read_document(filename, data) reads one uploaded file into {name, kind, pages, content, truncated}; the run makes
it a source shared by every section (like a YouTube video, see tools/video.py), cited and verified like a web page.

- PDF: the text layer of each page (pypdf), as one line per page starting with its page marker "[p. 3]", so a
  passage the writer or verifier picks can be traced back to its page (page_of). Line breaks inside a page are
  joined, since PDF lines end mid-sentence; words split by a hyphen at a line end are rejoined.
- .txt / .md: the text as written (UTF-8, or Latin-1 if it is not UTF-8); no pages.
- A PDF with no text layer (a scanned image) or an encrypted one is refused with the reason: Apollo does not run
  OCR and never guesses a document's content.

Text beyond DOCUMENT_MAX_CHARS is cut, and the result says so and up to which page it was read.
"""

import io
import re
from urllib.parse import quote

from core import config

PDF_TYPES = (".pdf",)
TEXT_TYPES = (".txt", ".md")
ACCEPTED = PDF_TYPES + TEXT_TYPES
_PAGE = re.compile(r"\[p\. (\d+)\]")


class DocumentUnreadable(Exception):
    """The file's text could not be read; the message says why, in plain language."""


def document_url(name: str) -> str:
    """Stand-in address for an attached file (it has no web address); shown as text, never as a link."""
    return "document://" + quote(name)


def page_of(content: str, passage: str) -> int | None:
    """The PDF page a passage of `content` comes from, or None (a text file, or a passage not found)."""
    probe = (passage or "").strip()[:80]
    at = content.find(probe) if probe else -1
    if at < 0:
        return None
    pages = list(_PAGE.finditer(content[: at + 10]))  # +10: the passage may start with its own marker
    return int(pages[-1].group(1)) if pages else None


def _clean(text: str) -> str:
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text or "")  # "verifi-\ncation" -> "verification"
    return re.sub(r"\s+", " ", text).strip()


def _pdf(data: bytes) -> tuple[list[str], int]:
    """(text of each page, page count)."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentUnreadable("it is password-protected")
        pages = [_clean(page.extract_text() or "") for page in reader.pages]
    except DocumentUnreadable:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise DocumentUnreadable(f"it is not a readable PDF ({type(exc).__name__})") from exc
    return pages, len(pages)


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def read_document(filename: str, data: bytes) -> dict:
    """Read one uploaded file. Raises DocumentUnreadable (with the reason) instead of returning guessed text."""
    name = (filename or "document").strip().replace("\\", "/").split("/")[-1][:120] or "document"
    lower = name.lower()
    if not lower.endswith(ACCEPTED):
        raise DocumentUnreadable(f"\"{name}\" is not a supported file type; attach a PDF, .txt or .md file")
    if len(data) > config.DOCUMENT_MAX_BYTES:
        raise DocumentUnreadable(f"\"{name}\" is larger than {config.DOCUMENT_MAX_BYTES // 1_000_000} MB")

    try:
        if lower.endswith(PDF_TYPES):
            texts, pages = _pdf(data)
            kind = "pdf"
            lines = [f"[p. {i}] {t}" for i, t in enumerate(texts, 1) if t]
        else:
            pages, kind = 0, "text"
            lines = [line.rstrip() for line in _text(data).replace("\r\n", "\n").split("\n")]
    except DocumentUnreadable as exc:
        raise DocumentUnreadable(f"Could not read \"{name}\": {exc}") from exc

    content = "\n".join(lines).strip()
    readable = len(_PAGE.sub("", content).strip())
    if readable < config.MIN_CONTENT_CHARS:
        why = ("it has no text layer (probably a scanned image); Apollo does not run OCR"
               if kind == "pdf" else "it has almost no text")
        raise DocumentUnreadable(f"Could not read \"{name}\": {why}")

    truncated = len(content) > config.DOCUMENT_MAX_CHARS
    if truncated:
        content = content[: config.DOCUMENT_MAX_CHARS].rsplit("\n", 1)[0] or content[: config.DOCUMENT_MAX_CHARS]
    read_to = page_of(content, content.rsplit("\n", 1)[-1]) if kind == "pdf" else None
    return {"name": name, "kind": kind, "pages": pages, "content": content, "truncated": truncated,
            "read_to_page": read_to if truncated else None}
