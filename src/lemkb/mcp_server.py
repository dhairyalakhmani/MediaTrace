"""MCP server: lets any AI agent (Claude Code, Claude Desktop, custom agents) query the
knowledge base as tools.

    python -m lemkb.mcp_server                      # stdio (for local agents)
    python -m lemkb.mcp_server --http --port 8765   # streamable HTTP (shared server)
"""
from __future__ import annotations

import argparse
from typing import Any

import json

import psycopg
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import analysis, service

INSTRUCTIONS = """\
Lemnisca media knowledge base: curated cell-culture media data (from MediaAssist) about
components, formulations, solubility, stability, uptake rates, transporters and
co-dependencies, each linked to the research paper it comes from.

Rules for answering from this server:
- Cite every claim with its DOI (and passage_id when quoting a paper).
- State the evidence level behind each claim: full_text (paper text available),
  abstract (only the abstract available) or metadata_only. Never present an
  abstract-level claim as if it had been checked against the full paper.
- If citation.doi_verified is false or citation.issue is set, say so.
- If nothing relevant is found, say that the knowledge base has no data; do not fill
  the gap from general knowledge without labelling it as such.

Typical flow: search_facts (structured data) -> evidence_for_fact (supporting passages)
-> get_paper / get_passages for more context. Use search_passages for free-text questions.

For anything the fixed tools don't cover (comparisons, rankings, aggregates, "all X where Y"),
call describe_data once, then query_data with a read-only SQL SELECT over the analysis views.
For charts ("plot solubility against temperature", "uptake rate by amino acid"), call plot with a
query that returns the x/y columns (and doi, so the chart can be cited). After a chart, state
what it shows, how many points were plotted or skipped, and cite the source DOIs.
You may add general scientific knowledge to explain results, but label it as general knowledge
and keep it separate from what the knowledge base says.
"""

server = MCPServer(name="lemnisca-media-kb", instructions=INSTRUCTIONS, version="0.1.0")


@server.tool()
def search_facts(component: str | None = None, query_type: str | None = None, text: str | None = None,
                 limit: int = 50) -> list[dict[str, Any]]:
    """Search MediaAssist facts.

    component: component name, case-insensitive; use % as a wildcard (e.g. "alanine", "%glut%").
    query_type: one of codependency, concentration, shared_transporter, solubility, stability,
                transporter, uptake_rate.
    text: free text matched anywhere in the fact or its citation (e.g. "CHO", "chemostat").
    Each result has the raw fact data, its citation (DOI, verified flag, issues) and the
    cited paper's evidence level.
    """
    return service.search_facts(component, query_type, text, limit)


@server.tool()
def list_components() -> list[dict[str, Any]]:
    """All component names in the knowledge base, with fact counts per query type."""
    return service.list_components()


@server.tool()
def evidence_for_fact(fact_id: int, limit: int = 5) -> dict[str, Any] | None:
    """One fact plus the passages of its cited paper that best support it (the 'proof')."""
    return service.evidence_for_fact(fact_id, limit)


@server.tool()
def search_passages(query: str, doi: str | None = None, kind: str | None = None,
                    evidence_level: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Ranked full-text search across paper passages.

    query: web-search syntax: words, "exact phrase", OR, -exclude (e.g. '"glutamine" stability -serum').
    doi: restrict to one paper. kind: title | abstract | body | caption | table.
    evidence_level: full_text | abstract | metadata_only.
    Returns passage_id (citable as <doi>#p<seq>), section, page, text and paper title.
    """
    return service.search_passages(query, doi, kind, evidence_level, limit)


@server.tool()
def get_paper(doi: str) -> dict[str, Any] | None:
    """Metadata, abstract, status, evidence level, license and a temporary file link for one paper."""
    return service.get_paper(doi)


@server.tool()
def get_passages(doi: str, section: str | None = None, start: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    """A paper's text in reading order. section: filter by section name (e.g. "method", "result")."""
    return service.get_passages(doi, section, start, limit)


@server.tool()
def describe_data() -> dict[str, Any]:
    """The analysis views (tables) available to query_data / plot, their columns, and usage tips.
    Call this once before writing SQL."""
    return analysis.describe_data()


@server.tool()
def query_data(sql: str, limit: int = 200) -> dict[str, Any]:
    """Run ONE read-only SQL SELECT (PostgreSQL) over the knowledge base and return the rows.

    Use for comparisons, rankings, aggregates and filters the other tools don't cover, e.g.
    SELECT component, max(solubility_value) FROM v_solubility GROUP BY component ORDER BY 2 DESC
    Views: v_concentration, v_solubility, v_stability, v_uptake_rate, v_transporter,
    v_shared_transporter, v_codependency, papers, passage_index, fact_evidence (see describe_data).
    Results are capped at 500 rows; include doi in the SELECT so answers can be cited.
    """
    try:
        return analysis.run_query(sql, limit)
    except (ValueError, psycopg.Error) as e:  # show the agent why, so it can fix its SQL
        raise ToolError(str(e).strip()) from e


@server.tool()
def plot(sql: str, x: str, y: str, kind: str = "scatter", group_by: str | None = None,
         title: str | None = None, x_label: str | None = None, y_label: str | None = None,
         log_x: bool = False, log_y: bool = False) -> list:
    """Draw a chart from a read-only SQL query and return it as an image, plus the plotted rows
    and their source DOIs.

    sql: a SELECT returning the x and y columns (and ideally doi / all_dois for citations).
    x, y: column names from that result. kind: scatter | line | bar (bar allows a text x).
    group_by: optional column whose values become separate series (e.g. component).
    Example: sql="SELECT component, temperature_c, solubility_value, doi FROM v_solubility
    WHERE solubility_unit ILIKE 'mg/L'", x="temperature_c", y="solubility_value", group_by="component"
    """
    try:
        png, meta = analysis.make_plot(sql, x, y, kind, group_by, title, x_label, y_label, log_x, log_y)
    except (ValueError, psycopg.Error) as e:
        raise ToolError(str(e).strip()) from e
    return [Image(data=png, format="png"), json.dumps(meta, ensure_ascii=False, default=str)]


@server.tool()
def coverage() -> dict[str, Any]:
    """How complete the knowledge base is: papers by status/evidence level, facts by type, top gaps."""
    return service.coverage()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.http:
        server.run("streamable-http", port=a.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
