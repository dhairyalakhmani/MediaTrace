"""Layer 4 query functions, shared by the MCP server (agents) and the REST API (apps).

Every answer carries its provenance: the citation, whether the DOI was verified, and
the paper's evidence level (full_text | abstract | metadata_only), so a consumer can
say exactly what a statement rests on.
"""
from __future__ import annotations

import re

from . import db, storage

_STOPWORDS = {"with", "that", "this", "were", "was", "from", "have", "been", "than", "their", "which", "showed",
              "shown", "when", "into", "also", "both", "other", "study", "cells", "cell", "medium", "levels"}

COMPONENT_KEYS = ["Component Name", "Selected component", "Other component", "Component 1", "Component 2",
                  "Selected Component", "Other Component"]
QUERY_TYPES = ["codependency", "concentration", "shared_transporter", "solubility", "stability",
               "transporter", "uptake_rate"]


def _cite(r: dict) -> dict:
    return {"position": r["citation_position"], "text": r["citation_text"], "url": r["citation_url"],
            "source_kind": r["source_kind"], "doi": r["doi"], "doi_verified": r["citation_verified"],
            "issue": r["citation_issue"],
            "paper": None if not r["doi"] else {"title": r["title"], "status": r["paper_status"],
                                                "evidence_level": r["evidence_level"]}}


def _with_all_citations(conn, facts: list[dict]) -> list[dict]:
    """Attach every cited source (a fact may cite several papers) as fact['citations']."""
    if facts:
        rows = conn.execute("SELECT * FROM fact_evidence WHERE row_id = ANY(%s) ORDER BY row_id, citation_position",
                            ([f["fact_id"] for f in facts],)).fetchall()
        by_fact: dict[int, list] = {}
        for r in rows:
            by_fact.setdefault(r["row_id"], []).append(_cite(r))
        for f in facts:
            f["citations"] = by_fact.get(f["fact_id"], [])
    return facts


def _fact(r: dict) -> dict:
    """One fact; 'citation'/'paper' describe its first cited source, 'citations' lists all."""
    return {
        "fact_id": r["row_id"],
        "query_type": r["query_type"],
        "data": r["data"],
        "citation": {
            "text": r["citation_text"], "url": r["citation_url"], "source_kind": r["source_kind"],
            "doi": r["doi"], "doi_verified": r["citation_verified"], "issue": r["citation_issue"],
        },
        "paper": None if not r["doi"] else {
            "title": r["title"], "status": r["paper_status"], "evidence_level": r["evidence_level"],
        },
    }


def search_facts(component: str | None = None, query_type: str | None = None, text: str | None = None,
                 limit: int = 50) -> list[dict]:
    """MediaAssist facts filtered by component name, query type and/or free text."""
    sql = ["SELECT * FROM fact_evidence WHERE coalesce(citation_position, 0) = 0"]
    args: dict = {"keys": COMPONENT_KEYS, "limit": min(limit, 500)}
    if component:
        sql.append("""AND EXISTS (SELECT 1 FROM jsonb_each_text(data) kv
                                  WHERE kv.key = ANY(%(keys)s) AND kv.value ILIKE %(component)s)""")
        args["component"] = component
    if query_type:
        sql.append("AND query_type = %(qt)s")
        args["qt"] = query_type
    if text:
        sql.append("AND (data::text ILIKE %(text)s OR citation_text ILIKE %(text)s)")
        args["text"] = f"%{text}%"
    sql.append("ORDER BY query_type, row_id LIMIT %(limit)s")
    with db.connect() as conn:
        return _with_all_citations(conn, [_fact(r) for r in conn.execute(" ".join(sql), args).fetchall()])


def list_components() -> list[dict]:
    """Every component name in the database, with how many facts of each type mention it."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT kv.value AS component, r.query_type, count(DISTINCT r.id) AS n
               FROM mediaassist_rows r, jsonb_each_text(r.data) kv
               WHERE kv.key = ANY(%s) AND kv.value IS NOT NULL
               GROUP BY 1, 2 ORDER BY 1, 2""",
            (COMPONENT_KEYS,),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["component"], {"component": r["component"], "facts": {}})["facts"][r["query_type"]] = r["n"]
    return list(out.values())


def get_paper(doi: str, with_file_link: bool = True) -> dict | None:
    with db.connect() as conn:
        p = conn.execute(
            """SELECT p.doi, p.title, p.year, p.journal, p.publisher, p.authors, p.abstract, p.status,
                      p.evidence_level, p.license, p.is_retracted, p.source, p.source_url, p.s3_key, p.sha256,
                      p.content_type, p.manual_url, p.retrieved_at,
                      (SELECT count(*) FROM row_citations c WHERE c.doi = p.doi) AS facts_citing,
                      (SELECT count(*) FROM passages s WHERE s.doi = p.doi) AS passages
               FROM papers p WHERE p.doi = %s""",
            (doi.lower(),),
        ).fetchone()
    if not p:
        return None
    p = dict(p)
    if with_file_link and p["s3_key"]:
        p["file_url"] = storage.presigned_url(p["s3_key"])  # temporary link (1 hour)
    return p


def search_passages(query: str, doi: str | None = None, kind: str | None = None,
                    evidence_level: str | None = None, limit: int = 10) -> list[dict]:
    """Ranked full-text search over paper passages (English stemming; supports "quoted phrases", OR, -exclude)."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT ps.doi || '#p' || ps.seq AS passage_id, ps.doi, ps.kind, ps.section, ps.page, ps.text,
                      p.title, p.year, p.evidence_level, round(ts_rank(ps.tsv, q)::numeric, 4) AS rank
               FROM passages ps JOIN papers p USING (doi), websearch_to_tsquery('english', %(q)s) q
               WHERE ps.tsv @@ q
                 AND (%(doi)s::text IS NULL OR ps.doi = %(doi)s)
                 AND (%(kind)s::text IS NULL OR ps.kind = %(kind)s)
                 AND (%(ev)s::text IS NULL OR p.evidence_level = %(ev)s)
               ORDER BY rank DESC, ps.doi, ps.seq LIMIT %(limit)s""",
            {"q": query, "doi": doi.lower() if doi else None, "kind": kind, "ev": evidence_level,
             "limit": min(limit, 100)},
        ).fetchall()
    return [dict(r, rank=float(r["rank"])) for r in rows]


def get_passages(doi: str, section: str | None = None, start: int = 0, limit: int = 50) -> list[dict]:
    """A paper's text in reading order (optionally one section)."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT doi || '#p' || seq AS passage_id, seq, kind, section, page, text FROM passages
               WHERE doi = %s AND (%s::text IS NULL OR section ILIKE %s) AND seq >= %s
               ORDER BY seq LIMIT %s""",
            (doi.lower(), section, f"%{section}%" if section else None, start, min(limit, 500)),
        ).fetchall()
    return [dict(r) for r in rows]


def evidence_for_fact(fact_id: int, limit: int = 5) -> dict | None:
    """A fact plus the passages of its cited paper that best support it."""
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM fact_evidence WHERE row_id = %s ORDER BY citation_position", (fact_id,)).fetchone()
    if not r:
        return None
    with db.connect() as conn:
        fact = _with_all_citations(conn, [_fact(r)])[0]
    terms = [v for k, v in (r["data"] or {}).items() if k in COMPONENT_KEYS and v]
    extra = {"uptake_rate": "uptake rate", "solubility": "solubility", "stability": "stability",
             "transporter": "transporter", "shared_transporter": "transporter", "concentration": "concentration",
             "codependency": ""}.get(r["query_type"], "")
    passages = []
    if r["doi"]:
        # Most specific first: every component (+ the property), then all components, then any.
        # Then rank by the fact's own wording (summary, condition, measure), since papers often
        # abbreviate component names (e.g. "Ala") that an exact-name search would miss.
        wording = " ".join(v for k, v in (r["data"] or {}).items()
                           if v and k not in COMPONENT_KEYS and not re.search(r"citation|source|link|year", k, re.I))
        words = list(dict.fromkeys(w for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", wording)
                                   if w.lower() not in _STOPWORDS))[:25]
        queries = [" ".join([*(f'"{t}"' for t in terms), extra]).strip(),
                   " ".join(f'"{t}"' for t in terms),
                   " OR ".join(words),
                   " OR ".join(f'"{t}"' for t in terms) or extra]
        for query in dict.fromkeys(q for q in queries if q):
            if passages := search_passages(query, doi=r["doi"], limit=limit):
                break
        if not passages:
            passages = get_passages(r["doi"], limit=3)  # at least title + abstract
    fact["supporting_passages"] = passages
    return fact


def coverage() -> dict:
    with db.connect() as conn:
        q = lambda sql: [dict(x) for x in conn.execute(sql).fetchall()]
        return {
            "papers_by_status": q("SELECT status, papers, pct FROM coverage_report"),
            "papers_by_evidence_level": q("SELECT evidence_level, count(*) n FROM papers GROUP BY 1 ORDER BY 2 DESC"),
            "facts_by_type": q("""SELECT r.query_type, count(*) n, count(*) FILTER (WHERE c.verified) doi_verified,
                                         count(*) FILTER (WHERE p.status = 'downloaded') with_full_text
                                  FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id
                                  LEFT JOIN papers p ON p.doi = c.doi GROUP BY 1 ORDER BY 1"""),
            "passages": conn.execute("SELECT count(*) n FROM passages").fetchone()["n"],
            "top_gaps": q("SELECT facts_citing, doi, title, status FROM gap_priority LIMIT 10"),
        }
