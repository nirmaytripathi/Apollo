"""Word (.docx) export of a finished run's report.

render_docx(state) builds the same content as the Markdown export (reports/export.py): the report body,
the cited references, and the claim verification table. It is rendered from the stored state, so it
works for any saved run. Only the Markdown the report writer produces is handled: headings (#, ##, ###),
bullet and numbered lists, paragraphs, **bold** and *italic*. Citations such as [S3] stay as written.
"""

import io
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.shared import Pt, RGBColor

from tools.text import CLAIM_STATUSES

_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
# Characters XML 1.0 cannot hold (control characters other than tab and newlines); scraped text sometimes has them.
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MUTED = RGBColor(0x5B, 0x67, 0x7A)


def _clean(text: str) -> str:
    return _BAD_XML.sub("", str(text or ""))


def _runs(paragraph, text: str) -> None:
    """Add `text` to the paragraph with **bold**, *italic* and `code` spans; [label](url) becomes "label (url)"."""
    text = _LINK.sub(r"\1 (\2)", _clean(text))
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            paragraph.add_run(part[1:-1]).font.name = "Consolas"
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            paragraph.add_run(part[1:-1]).italic = True
        else:
            paragraph.add_run(part)


def _markdown(doc, md: str) -> None:
    para: list[str] = []

    def flush() -> None:
        if para:
            _runs(doc.add_paragraph(), " ".join(s.strip() for s in para))
            para.clear()

    for line in _clean(md).splitlines():
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if not stripped or re.fullmatch(r"[-*_]{3,}", stripped):
            flush()
        elif heading:
            flush()
            level = min(len(heading.group(1)), 3)
            doc.add_heading(heading.group(2).strip().strip("#").strip(), level=level)
        elif bullet:
            flush()
            _runs(doc.add_paragraph(style="List Bullet"), bullet.group(1))
        elif numbered:
            flush()
            _runs(doc.add_paragraph(style="List Number"), numbered.group(1))
        else:
            para.append(stripped)
    flush()


def render_docx(state: dict) -> bytes:
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    report = state.get("report") or ""
    if not re.match(r"^\s*#\s", report):  # the report normally opens with its own "# Title"
        doc.add_heading(_clean(state.get("topic") or "Research report"), level=0)
    _markdown(doc, report)

    doc.add_heading("References", level=1)
    refs = state.get("references") or []
    for r in refs:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(f"[{r['id']}] ").bold = True
        p.add_run(_clean(r.get("title") or r.get("url") or ""))
        if r.get("url"):
            url = p.add_run(f" {_clean(r['url'])}")
            url.font.color.rgb = _MUTED
    if not refs:
        doc.add_paragraph("No sources were cited.").runs[0].italic = True

    claims = state.get("claims") or []
    counts = {k: sum(c["status"] == k for c in claims) for k in CLAIM_STATUSES}
    doc.add_heading("Claim verification summary", level=1)
    doc.add_paragraph(
        "Each cited sentence was checked against the retrieved text of the source(s) it cites. "
        + ", ".join(f"{k}: {n}" for k, n in counts.items() if n or k in ("verified", "partial", "not_found"))
        + "."
    )
    if claims:
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for cell, title in zip(table.rows[0].cells, ("#", "Claim", "Sources", "Status")):
            cell.text = ""
            cell.paragraphs[0].add_run(title).bold = True
        for n, c in enumerate(claims, 1):
            row = table.add_row().cells
            row[0].text = str(n)
            row[1].text = _clean(c.get("claim"))
            row[2].text = ", ".join(c.get("source_ids") or [])
            row[3].text = c.get("status") or ""

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
