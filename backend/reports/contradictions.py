"""Contradiction map: Hermes' contradictions as an interactive HTML graph.

build_graph(contradictions, findings) -> networkx.Graph
    nodes = source ids (hover: title + URL), edges = conflicts (label: the claim).
    Several conflicts between the same two sources are merged into one edge.

write_contradiction_map(contradictions, findings, path) -> str message
    Writes a pyvis HTML file with the JS inlined (works offline). With no
    contradictions it writes a plain page saying "No conflicts found" instead
    of an empty graph.

The contradictions themselves come only from Hermes' JSON output; this module
does no detection of its own.
"""

import html
from pathlib import Path

import networkx as nx

NO_CONFLICTS = "No conflicts found between the retrieved sources."

# The map is shown inside an iframe (and opened on its own from report exports), so the graph fills
# the viewport instead of a fixed 650 px canvas that the frame cuts off, with no page margins or
# scrollbars, and no in-page heading (the surrounding page already has one).
_FILL_VIEWPORT = (
    "<style>html,body{margin:0;height:100%;overflow:hidden;background:#fff}"
    ".card{border:0!important;margin:0!important;height:100%}"
    "#mynetwork{height:100vh!important;border:0!important}</style>"
)


def build_graph(contradictions: list[dict], findings: list[dict]) -> nx.Graph:
    by_id = {f["id"]: f for f in findings}
    g = nx.Graph()
    for c in contradictions:
        for sid in (c["source_a"], c["source_b"]):
            f = by_id.get(sid, {})
            g.add_node(sid, label=sid, title=f"{f.get('title', '')}\n{f.get('url', '')}", shape="dot", size=22,
                       color="#4c9aff", font={"size": 16, "face": "monospace"})
        a, b = c["source_a"], c["source_b"]
        if g.has_edge(a, b):
            g[a][b]["title"] += f"\n{c['claim']}"
        else:
            # length: room for the edge label between the two nodes
            g.add_edge(a, b, title=c["claim"], label=c["claim"][:40] + ("…" if len(c["claim"]) > 40 else ""), color="#ef6673",
                       width=3, length=320, font={"size": 12, "align": "top", "background": "#ffffff"})
    return g


def write_contradiction_map(contradictions: list[dict], findings: list[dict], path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not contradictions:
        path.write_text(
            "<!doctype html><meta charset='utf-8'><title>Contradiction map</title>"
            "<body style='font-family:sans-serif;padding:2rem'>"
            f"<h1>Contradiction map</h1><p>{html.escape(NO_CONFLICTS)}</p></body>",
            encoding="utf-8",
        )
        return NO_CONFLICTS

    from pyvis.network import Network

    g = build_graph(contradictions, findings)
    net = Network(height="100vh", width="100%", cdn_resources="in_line")
    net.from_nx(g)
    html_text = net.generate_html().replace("</head>", _FILL_VIEWPORT + "</head>", 1)
    # Write ourselves as UTF-8: pyvis' own writer uses the OS default encoding (cp1252 on Windows).
    path.write_text(html_text, encoding="utf-8")
    return f"{g.number_of_edges()} conflict(s) between {g.number_of_nodes()} sources"
