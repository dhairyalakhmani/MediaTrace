"""Resolve every source a MediaAssist row cites to a verified DOI.

MediaAssist rows carry free-text citations plus links (DOI URL, PubMed, publisher page,
PubChem, ...). A row may cite several sources, joined with "|" (links joined with ", ").
The DOI written in a citation is not always right: one export cites a 2018 paper as
bit.26794, bit.26795, bit.26796 and bit.26813 (spreadsheet drag-fill). So for every
cited source we:

  1. collect candidate DOIs from its text, its link, and PubMed IDs in the link
  2. look up each candidate's registered title (Crossref) and keep the one whose title
     matches the citation text; verified = score >= MATCH_THRESHOLD
  3. if that yields no verified DOI, link to a paper already in our vault whose title
     matches the citation (stored papers are trusted, e.g. uploaded and confirmed by a
     person); this repairs wrong or missing DOIs
  4. still nothing: Crossref bibliographic search, accepted only on a near-exact title match
  5. record every candidate and any disagreement in row_citations.issue

Only chosen DOIs are registered as papers, so a typo never pulls in the wrong paper.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

import httpx

from . import db
from .config import settings
from .doi import find_all

log = logging.getLogger("lemkb.citations")

MATCH_THRESHOLD = 0.75
SEARCH_THRESHOLD = 0.9    # stricter when the DOI came from a bibliographic search, not the source
CATALOG_THRESHOLD = 0.9   # linking a citation to one of our stored papers by its title alone
CATALOG_MARGIN = 0.1      # ...and it must clearly beat the next-best stored paper
_CITATION_KEY = re.compile(r"^\s*(citation|source|reference)s?\s*$", re.I)
_LINK_KEY = re.compile(r"^\s*(citation|source|reference)s?\s+link\s*$", re.I)
_PMID_URL = re.compile(r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|ncbi\.nlm\.nih\.gov/pubmed/)(\d{4,9})")
_URL_SPLIT = re.compile(r"\s*\|\s*|,\s*(?=https?://)")
_STOP = {"the", "and", "for", "with", "from", "into", "by", "of", "on", "in", "an", "a", "to", "its", "via", "at", "as"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2 and t not in _STOP}


def clean_title(title: str | None) -> str:
    """Registered titles can carry markup and footnotes: 'Vitamin B<sub>6</sub>...<sup>1</sup>',
    'Solutions**Research Department of ...'. Keep only the title itself."""
    t = re.sub(r"<sup>.*?</sup>", "", title or "", flags=re.S)   # footnote markers
    t = re.sub(r"<[^>]+>", " ", t)                                # other tags -> space, keeping their text
    return re.split(r"\*{1,2}(?=[A-Z])", t)[0].strip()             # "...Solutions**Research Dept..."


def title_match(title: str | None, citation_text: str | None) -> float:
    """Share of the title's words that appear in the text (0..1).

    Some PDF text layers drop the spaces between words ("Diabetes-inducedactivationof...");
    for such glued text the title must appear as one contiguous run of characters.
    """
    title = clean_title(title)
    t = _tokens(title or "")
    if not t:
        return 0.0
    text = citation_text or ""
    words = len(t & _tokens(text)) / len(t)
    if words >= 0.75 or text.count(" ") > len(text) / 25:
        return round(words, 3)  # normally spaced text: the word overlap is the answer
    # Glued text: require the title as one contiguous run of characters, which is far more
    # specific than finding its individual words somewhere in the text.
    from difflib import SequenceMatcher
    st = re.sub(r"[^a-z0-9]", "", (title or "").lower())
    sx = re.sub(r"[^a-z0-9]", "", text.lower())
    if not st or not sx:
        return round(words, 3)
    m = SequenceMatcher(None, sx, st, autojunk=False).find_longest_match(0, len(sx), 0, len(st))
    return round(max(words, m.size / len(st)), 3)


def split_citation(row: dict) -> tuple[str | None, str | None]:
    text = next((v for k, v in row.items() if v and _CITATION_KEY.match(k)), None)
    url = next((v for k, v in row.items() if v and _LINK_KEY.match(k)), None)
    return text, url


def split_parts(text: str | None, url: str | None) -> list[tuple[str | None, str | None]]:
    """'A | B | C' + 'urlA, urlB, urlC' -> [(A, urlA), (B, urlB), (C, urlC)].

    Links are paired by position only when the counts agree; otherwise a single link
    goes to the first source and the rest resolve from their own text.
    """
    parts = [p.strip() for p in (text or "").split("|") if p.strip()]
    if len(parts) <= 1:
        return [(text, url)]
    urls = [u.strip() for u in _URL_SPLIT.split(url or "") if u.strip()]
    if len(urls) == len(parts):
        return list(zip(parts, urls))
    return [(p, urls[0] if i == 0 and len(urls) == 1 else None) for i, p in enumerate(parts)]


class _Registry:
    """Cached lookups: DOI -> registered title, PMID -> DOI."""

    def __init__(self, client: httpx.Client):
        self.c = client
        self.titles: dict[str, str | None] = {}
        self.exists: dict[str, bool] = {}
        self.pmids: dict[str, str | None] = {}

    def title(self, doi: str) -> str | None:
        if doi not in self.titles:
            params = {"mailto": settings.contact_email} if settings.contact_email else None
            try:
                r = self.c.get(f"https://api.crossref.org/works/{quote(doi, safe='')}", params=params)
                ok = r.status_code == 200
                self.exists[doi] = ok or r.status_code not in (404, 400)
                self.titles[doi] = ((r.json()["message"].get("title") or [None])[0]) if ok else None
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.warning("crossref lookup failed for %s: %s", doi, e)
                self.exists[doi], self.titles[doi] = True, None  # unknown, not proven missing
        return self.titles[doi]

    def search(self, citation_text: str) -> tuple[str, str] | None:
        """Crossref bibliographic search: citation text -> (doi, title) of the top hit."""
        params = {"query.bibliographic": citation_text[:500], "rows": 1, "select": "DOI,title"}
        if settings.contact_email:
            params["mailto"] = settings.contact_email
        try:
            items = self.c.get("https://api.crossref.org/works", params=params).json()["message"]["items"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("crossref search failed: %s", e)
            return None
        if not items:
            return None
        doi, title = items[0]["DOI"].lower(), (items[0].get("title") or [None])[0]
        self.titles.setdefault(doi, title)
        self.exists.setdefault(doi, True)
        return doi, title

    def doi_for_pmid(self, pmid: str) -> str | None:
        if pmid not in self.pmids:
            try:
                r = self.c.get(
                    "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                    params={"query": f"EXT_ID:{pmid} AND SRC:MED", "format": "json", "resultType": "lite"},
                )
                res = (r.json().get("resultList") or {}).get("result") or []
                self.pmids[pmid] = ((res[0].get("doi") or "").lower() or None) if res else None
            except (httpx.HTTPError, ValueError) as e:
                log.warning("pmid lookup failed for %s: %s", pmid, e)
                self.pmids[pmid] = None
        return self.pmids[pmid]


def _resolve_by_doi(reg: _Registry, text: str | None, url: str | None) -> dict:
    cands: list[tuple[str, str]] = [(d, "citation_text") for d in find_all(text or "")]
    cands += [(d, "link_doi") for d in find_all(url or "")]
    if url and (m := _PMID_URL.search(url)):
        if d := reg.doi_for_pmid(m.group(1)):
            cands.append((d, "link_pmid"))

    seen, scored = set(), []
    for d, via in cands:
        if d in seen:
            continue
        seen.add(d)
        title = reg.title(d)
        scored.append({"doi": d, "via": via, "title": title, "registered": reg.exists.get(d, True),
                       "score": title_match(title, text)})

    out = {"citation_text": text, "citation_url": url, "candidates": scored, "doi": None,
           "resolved_via": None, "title_match": None, "verified": False, "issue": None,
           "source_kind": "unresolved"}
    if not scored:
        if text or url:
            out.update(source_kind="non_doi", issue=None if url else "no DOI or link given")
        return out

    live = [s for s in scored if s["registered"]]
    if not live:
        out["issue"] = "; ".join(f"DOI {s['doi']} is not registered" for s in scored)
        return out

    best = max(live, key=lambda s: (s["score"], s["via"] != "citation_text"))
    verified = best["score"] >= MATCH_THRESHOLD
    issues = [f"DOI {s['doi']} is not registered" for s in scored if not s["registered"]]
    for s in live:
        if s is not best and s["via"] == "citation_text" and s["score"] < MATCH_THRESHOLD <= best["score"]:
            issues.append(f"citation text DOI {s['doi']} is a different paper ({s['title']!r}); "
                          f"used {best['doi']} from {best['via']}")
    if not verified:
        issues.append(f"registered title of {best['doi']} does not match citation text (score {best['score']})")
    out.update(source_kind="paper", doi=best["doi"], resolved_via=best["via"], title_match=best["score"],
               verified=verified, issue="; ".join(issues) or None)
    return out


def _catalog_match(catalog: list[dict], text: str | None) -> tuple[dict | None, float, float]:
    """Best stored paper whose title appears in the citation text: (paper, score, runner-up score)."""
    if not text or not catalog:
        return None, 0.0, 0.0
    scored = sorted(((title_match(p["title"], text), p) for p in catalog), key=lambda x: -x[0])
    return scored[0][1], scored[0][0], (scored[1][0] if len(scored) > 1 else 0.0)


def _search_fallback(reg: _Registry, text: str, url: str | None, out: dict) -> dict:
    if hit := reg.search(text):
        score = title_match(hit[1], text)
        out["candidates"] = [{"doi": hit[0], "via": "crossref_search", "title": hit[1], "registered": True, "score": score}]
        if score >= SEARCH_THRESHOLD:
            out.update(source_kind="paper", doi=hit[0], resolved_via="crossref_search", title_match=score,
                       verified=True, issue=None)
        else:
            out.update(source_kind="non_doi" if url else "unresolved",
                       issue=f"no DOI given; closest Crossref match {hit[0]} rejected (score {score})")
    return out


def resolve_part(reg: _Registry, text: str | None, url: str | None, catalog: list[dict] | None = None) -> dict:
    """Resolve one cited source (see the module docstring for the order of steps)."""
    res = _resolve_by_doi(reg, text, url)
    paper, score, runner_up = _catalog_match(catalog or [], text)
    strong = paper is not None and score >= CATALOG_THRESHOLD and score - runner_up >= CATALOG_MARGIN
    stored = {c["doi"] for c in catalog or []}
    # Use the stored paper when the DOI route failed, or found a paper we don't hold while
    # a stored paper clearly carries this exact title (the citation's DOI is then wrong).
    if strong and paper["doi"] != res["doi"] and (not res["verified"] or res["doi"] not in stored):
        note = (f"linked by title to stored paper {paper['doi']} (score {score})"
                + (f"; the DOI resolved from the citation was {res['doi']}" if res["doi"] else
                   "; the citation gave no usable DOI"))
        res.update(source_kind="paper", doi=paper["doi"], resolved_via="catalog_title", title_match=score,
                   verified=True, issue=note)
        return res
    if not res["verified"] and text:
        # No DOI given, or the given DOI belongs to another paper (e.g. off by one):
        # look the citation up by its text instead.
        found = _search_fallback(reg, text, url, dict(res, candidates=list(res["candidates"])))
        if found["verified"]:
            wrong = res["doi"]
            found["candidates"] = res["candidates"] + found["candidates"]
            found["issue"] = (f"cited DOI {wrong} is a different paper; found {found['doi']} by searching the citation"
                              if wrong else None)
            return found
        if not res["candidates"]:
            return found
    return res


def resolve_row(reg: _Registry, row: dict, catalog: list[dict] | None = None) -> list[dict]:
    """All cited sources of one MediaAssist row, in order (position 0 = first cited)."""
    text, url = split_citation(row)
    return [dict(resolve_part(reg, t, u, catalog), position=i) for i, (t, u) in enumerate(split_parts(text, url))]


def load_catalog(conn) -> list[dict]:
    """Stored papers (full text in the vault) are trusted link targets."""
    return [dict(r) for r in conn.execute(
        "SELECT doi, title FROM papers WHERE status = 'downloaded' AND title IS NOT NULL").fetchall()]


def resolve(only_unresolved: bool = True, problems_only: bool = False) -> dict[str, int]:
    """Resolve every cited source of MediaAssist rows and register the chosen DOIs as papers.

    only_unresolved: rows never resolved. problems_only: rows with an unverified or DOI-less citation.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.data FROM mediaassist_rows r
               WHERE (%(problems)s AND EXISTS (SELECT 1 FROM row_citations c WHERE c.row_id = r.id AND NOT c.verified))
                  OR (NOT %(problems)s AND (%(only)s = FALSE
                      OR NOT EXISTS (SELECT 1 FROM row_citations c WHERE c.row_id = r.id)))
               ORDER BY r.id""",
            {"only": only_unresolved, "problems": problems_only},
        ).fetchall()
        catalog = load_catalog(conn)
        counts = {"rows": len(rows), "citations": 0, "verified": 0, "unverified": 0, "non_doi": 0,
                  "unresolved": 0, "with_issue": 0, "linked_by_title_to_stored_paper": 0}
        chosen: list[str] = []
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "lemkb/0.1"}) as c:
            reg = _Registry(c)
            for r in rows:
                parts = resolve_row(reg, r["data"], catalog)
                with conn.transaction():
                    conn.execute("DELETE FROM row_citations WHERE row_id = %s", (r["id"],))
                    for res in parts:
                        conn.execute(
                            """INSERT INTO row_citations (row_id, position, citation_text, citation_url, source_kind, doi,
                                                          resolved_via, title_match, verified, candidates, issue, resolved_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())""",
                            (r["id"], res["position"], res["citation_text"], res["citation_url"], res["source_kind"],
                             res["doi"], res["resolved_via"], res["title_match"], res["verified"],
                             json.dumps(res["candidates"]), res["issue"]),
                        )
                for res in parts:
                    if res["doi"]:
                        chosen.append(res["doi"])
                    key = ("verified" if res["verified"] else "unverified") if res["source_kind"] == "paper" else res["source_kind"]
                    counts[key] += 1
                    counts["citations"] += 1
                    counts["with_issue"] += bool(res["issue"])
                    counts["linked_by_title_to_stored_paper"] += res["resolved_via"] == "catalog_title"
        counts["new_papers"] = db.register_dois(conn, chosen)
    return counts
