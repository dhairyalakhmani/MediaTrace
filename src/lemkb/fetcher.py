"""Automation B: DOI -> legal full text -> S3, with every step recorded in Postgres.

For each DOI we collect metadata and candidate file URLs from open, legal indexes,
then try candidates in priority order until one yields a valid file:

    1. Europe PMC        (PMC copies; PDF render + OA XML)
    2. Unpaywall         (best legal OA location; needs CONTACT_EMAIL)
    3. OpenAlex          (OA locations incl. repositories)
    4. Semantic Scholar  (openAccessPdf)
    5. CORE              (institutional repositories; needs CORE_API_KEY)
    6. Publisher TDM     (Wiley, Elsevier: only with a token issued under a commercial
                          TDM agreement; WILEY_TDM_TOKEN / ELSEVIER_API_KEY)
    7. PMC cloud         (NCBI's public AWS dataset: PDFs where present; JATS XML
                          as the fallback when no PDF could be fetched)

Outcome per DOI (papers.status):
    downloaded       file stored in S3, hash + source URL recorded
    not_available    no legal copy found anywhere -> gap list
    pending_manual   a free copy exists but blocks bots -> manual_url for a person
    download_failed  candidates existed but none could be fetched (retryable)
    invalid_doi      DOI unknown to Crossref and every index
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import httpx

from . import db, storage
from .config import settings
from .doi import s3_key

log = logging.getLogger("lemkb.fetch")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 lemkb/0.1"
)
MAX_BYTES = 150 * 1024 * 1024


@dataclass
class Candidate:
    source: str
    url: str
    kind: str = "pdf"  # pdf | xml
    license: str | None = None
    headers: dict[str, str] | None = None
    full_text_marker: bytes | None = None  # must appear in the body (guards against abstract-only responses)


@dataclass
class Lookup:
    meta: dict = field(default_factory=dict)       # merged, normalized metadata
    raw: dict = field(default_factory=dict)        # raw responses per source (provenance)
    candidates: list[Candidate] = field(default_factory=list)
    abstracts: dict[str, str] = field(default_factory=dict)  # source -> abstract text
    found_in_any_index: bool = False


class _RateLimiter:
    """Minimum spacing between requests to the same host, shared across threads."""

    def __init__(self, spacing: dict[str, float], default: float = 0.2):
        self.spacing, self.default = spacing, default
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        gap = self.spacing.get(host, self.default)
        with self.lock:
            now = time.monotonic()
            ready = self.last.get(host, 0) + gap
            self.last[host] = max(now, ready)
        if ready > now:
            time.sleep(ready - now)


# Wiley TDM allows 60 requests / 10 min; Elsevier allows more, but stay polite.
_limiter = _RateLimiter({"api.semanticscholar.org": 1.2, "api.core.ac.uk": 1.0,
                         "api.wiley.com": 10.5, "api.elsevier.com": 1.0})


ABSTRACT_PRIORITY = ("europepmc", "crossref", "openalex", "semanticscholar")


def _clean_abstract(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)                  # JATS / HTML tags
    text = re.sub(r"^\s*(abstract|summary)[:.\s]+", "", text.strip(), flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) > 40 else None


def _add_abstract(out: "Lookup", source: str, text: str | None) -> None:
    if cleaned := _clean_abstract(text):
        out.abstracts[source] = cleaned


def _http() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA, "Accept": "*/*"},
        follow_redirects=True,
        timeout=httpx.Timeout(60, connect=20),
    )


def _get_json(client: httpx.Client, url: str, **kw) -> dict | None:
    for attempt in range(3):
        _limiter.wait(url)
        try:
            r = client.get(url, **kw)
        except httpx.HTTPError as e:
            log.debug("GET %s failed: %s", url, e)
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Metadata + candidate discovery
# ---------------------------------------------------------------------------

def _crossref(c: httpx.Client, doi: str, out: Lookup) -> None:
    params = {"mailto": settings.contact_email} if settings.contact_email else None
    j = _get_json(c, f"https://api.crossref.org/works/{quote(doi, safe='')}", params=params)
    if not j:
        return
    out.found_in_any_index = True
    m = j.get("message", {})
    out.raw["crossref"] = {k: m.get(k) for k in ("title", "container-title", "publisher", "issued", "author", "license", "update-to")}
    out.meta.setdefault("title", (m.get("title") or [None])[0])
    out.meta.setdefault("journal", (m.get("container-title") or [None])[0])
    out.meta.setdefault("publisher", m.get("publisher"))
    parts = (m.get("issued") or {}).get("date-parts") or [[None]]
    out.meta.setdefault("year", parts[0][0] if parts[0] else None)
    _add_abstract(out, "crossref", m.get("abstract"))
    if m.get("author"):
        out.meta.setdefault("authors", [
            " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name") for a in m["author"]
        ])


def _europepmc(c: httpx.Client, doi: str, out: Lookup) -> None:
    j = _get_json(
        c,
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "core", "pageSize": 1},
    )
    results = ((j or {}).get("resultList") or {}).get("result") or []
    if not results:
        return
    r = results[0]
    out.found_in_any_index = True
    out.raw["europepmc"] = {k: r.get(k) for k in ("id", "source", "pmid", "pmcid", "isOpenAccess", "inEPMC", "hasPDF", "license")}
    out.meta.setdefault("title", r.get("title"))
    out.meta.setdefault("year", int(r["pubYear"]) if r.get("pubYear", "").isdigit() else None)
    out.meta.setdefault("pmid", r.get("pmid"))
    _add_abstract(out, "europepmc", r.get("abstractText"))
    pmcid = r.get("pmcid")
    if pmcid and (r.get("inEPMC") == "Y" or r.get("isOpenAccess") == "Y"):
        out.candidates.append(Candidate("europepmc", f"https://europepmc.org/articles/{pmcid}?pdf=render", "pdf", r.get("license")))
    for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
        if u.get("documentStyle") == "pdf" and u.get("availabilityCode") in ("OA", "F"):
            out.candidates.append(Candidate("europepmc", u["url"], "pdf", r.get("license")))
    if pmcid:
        out.meta.setdefault("pmcid", pmcid)


def _unpaywall(c: httpx.Client, doi: str, out: Lookup) -> None:
    if not settings.contact_email:
        return
    j = _get_json(c, f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}", params={"email": settings.contact_email})
    if not j:
        return
    out.found_in_any_index = True
    out.raw["unpaywall"] = {k: j.get(k) for k in ("is_oa", "oa_status", "journal_name", "publisher", "year", "best_oa_location")}
    out.meta.setdefault("is_oa", j.get("is_oa"))
    out.meta.setdefault("oa_status", j.get("oa_status"))
    out.meta.setdefault("title", j.get("title"))
    out.meta.setdefault("journal", j.get("journal_name"))
    out.meta.setdefault("year", j.get("year"))
    locs = [j.get("best_oa_location")] + (j.get("oa_locations") or [])
    for loc in filter(None, locs):
        if loc.get("url_for_pdf"):
            out.candidates.append(Candidate("unpaywall", loc["url_for_pdf"], "pdf", loc.get("license")))
            out.meta.setdefault("license", loc.get("license"))


def _openalex(c: httpx.Client, doi: str, out: Lookup) -> None:
    params = {"mailto": settings.contact_email} if settings.contact_email else None
    j = _get_json(c, f"https://api.openalex.org/works/https://doi.org/{doi}", params=params)
    if not j:
        return
    out.found_in_any_index = True
    oa = j.get("open_access") or {}
    out.raw["openalex"] = {"id": j.get("id"), "is_retracted": j.get("is_retracted"), "open_access": oa}
    out.meta["is_retracted"] = bool(j.get("is_retracted"))
    if pmcid := ((j.get("ids") or {}).get("pmcid") or "").rsplit("/", 1)[-1]:
        out.meta.setdefault("pmcid", pmcid.upper())
    out.meta.setdefault("title", j.get("title"))
    out.meta.setdefault("year", j.get("publication_year"))
    out.meta.setdefault("is_oa", oa.get("is_oa"))
    out.meta.setdefault("oa_status", oa.get("oa_status"))
    if inv := j.get("abstract_inverted_index"):
        words = sorted((pos, w) for w, positions in inv.items() for pos in positions)
        _add_abstract(out, "openalex", " ".join(w for _, w in words))
    src = ((j.get("primary_location") or {}).get("source") or {})
    out.meta.setdefault("journal", src.get("display_name"))
    locs = [j.get("best_oa_location")] + (j.get("locations") or [])
    for loc in filter(None, locs):
        if loc.get("pdf_url") and loc.get("is_oa"):
            out.candidates.append(Candidate("openalex", loc["pdf_url"], "pdf", loc.get("license")))
            out.meta.setdefault("license", loc.get("license"))


def _semantic_scholar(c: httpx.Client, doi: str, out: Lookup) -> None:
    j = _get_json(c, f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}", params={"fields": "title,year,abstract,openAccessPdf"})
    if not j:
        return
    out.found_in_any_index = True
    _add_abstract(out, "semanticscholar", j.get("abstract"))
    pdf = (j.get("openAccessPdf") or {}).get("url")
    if pdf:
        out.candidates.append(Candidate("semanticscholar", pdf, "pdf"))


def _core(c: httpx.Client, doi: str, out: Lookup) -> None:
    if not settings.core_api_key:
        return
    j = _get_json(
        c, "https://api.core.ac.uk/v3/search/works",
        params={"q": f'doi:"{doi}"', "limit": 3},
        headers={"Authorization": f"Bearer {settings.core_api_key}"},
    )
    for w in (j or {}).get("results") or []:
        if w.get("downloadUrl"):
            out.candidates.append(Candidate("core", w["downloadUrl"], "pdf"))


WILEY_PREFIXES = ("10.1002/", "10.1111/", "10.1046/", "10.1113/", "10.1096/")
ELSEVIER_PREFIXES = ("10.1016/", "10.1053/", "10.1054/")


def _publisher_is(out: Lookup, doi: str, name: str, prefixes: tuple[str, ...]) -> bool:
    return doi.startswith(prefixes) or name in (out.meta.get("publisher") or "").lower()


def _wiley_tdm(c: httpx.Client, doi: str, out: Lookup) -> None:
    """Wiley TDM API. Returns the PDF for content the token's account is entitled to."""
    if settings.wiley_tdm_token and _publisher_is(out, doi, "wiley", WILEY_PREFIXES):
        out.candidates.append(Candidate(
            "wiley_tdm", f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{quote(doi, safe='')}", "pdf",
            "wiley-tdm-agreement", headers={"Wiley-TDM-Client-Token": settings.wiley_tdm_token},
        ))


def _elsevier_tdm(c: httpx.Client, doi: str, out: Lookup) -> None:
    """Elsevier Article Retrieval API, full-text XML.

    Without entitlement Elsevier still answers 200, but with the abstract only (and a
    PDF request returns just page 1), so we request XML and require <originalText>.
    """
    if settings.elsevier_api_key and _publisher_is(out, doi, "elsevier", ELSEVIER_PREFIXES):
        headers = {"X-ELS-APIKey": settings.elsevier_api_key, "Accept": "text/xml"}
        if settings.elsevier_insttoken:
            headers["X-ELS-Insttoken"] = settings.elsevier_insttoken
        out.candidates.append(Candidate(
            "elsevier_tdm", f"https://api.elsevier.com/content/article/doi/{quote(doi, safe='/')}?view=FULL", "xml",
            "elsevier-tdm-agreement", headers=headers, full_text_marker=b"<originalText",
        ))


PMC_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"


def _pmc_cloud(c: httpx.Client, doi: str, out: Lookup) -> None:
    """NCBI's PMC Article Datasets on AWS: the sanctioned bulk route for PMC full text.

    Covers the OA subset and author manuscripts; each article's license_code tells us
    whether it may be text-mined (e.g. CC BY, CC BY-NC, TDM).
    """
    pmcid = out.meta.get("pmcid")
    if not pmcid:
        return
    _limiter.wait(PMC_BUCKET)
    r = c.get(f"{PMC_BUCKET}/", params={"list-type": "2", "prefix": f"{pmcid}.", "max-keys": "200"})
    if r.status_code != 200:
        return
    keys = re.findall(r"<Key>([^<]+)</Key>", r.text)
    if not keys:
        return
    latest = max({k.split("/", 1)[0] for k in keys}, key=lambda v: int(v.rsplit(".", 1)[-1]))
    meta = _get_json(c, f"{PMC_BUCKET}/{latest}/{latest}.json") or {}
    out.raw["pmc_cloud"] = meta
    lic = meta.get("license_code")
    if meta.get("is_retracted"):
        out.meta["is_retracted"] = True
    for k in keys:
        if k.startswith(latest + "/") and k.lower().endswith(".pdf"):
            out.candidates.append(Candidate("pmc_cloud", f"{PMC_BUCKET}/{k}", "pdf", lic))
    out.raw["_pmc_xml"] = (f"{PMC_BUCKET}/{latest}/{latest}.xml", lic)


def discover(c: httpx.Client, doi: str) -> Lookup:
    out = Lookup()
    for fn in (_crossref, _europepmc, _unpaywall, _openalex, _pmc_cloud, _semantic_scholar, _core,
               _wiley_tdm, _elsevier_tdm):
        try:
            fn(c, doi, out)
        except Exception as e:  # one bad source must never stop the others
            log.warning("%s: %s lookup error: %s", doi, fn.__name__, e)
    # Structured XML is the fallback when no PDF can be fetched: still full text, still citable.
    if xml := out.raw.pop("_pmc_xml", None):
        out.candidates.append(Candidate("pmc_cloud_xml", xml[0], "xml", xml[1]))
    # de-duplicate candidates by URL, keeping priority order
    seen, uniq = set(), []
    for cand in out.candidates:
        if cand.url not in seen:
            seen.add(cand.url)
            uniq.append(cand)
    out.candidates = uniq
    return out


# ---------------------------------------------------------------------------
# Download + validate
# ---------------------------------------------------------------------------

def _looks_valid(kind: str, data: bytes, marker: bytes | None = None) -> bool:
    if marker is not None and marker not in data:
        return False
    if kind == "pdf":
        return b"%PDF" in data[:1024] and len(data) > 1000
    head = data[:2000].lstrip().lower()
    return head.startswith(b"<") and (marker is not None or b"<article" in data[:20000].lower())


def _download(c: httpx.Client, cand: Candidate) -> tuple[bytes | None, int | None, str]:
    _limiter.wait(cand.url)
    try:
        with c.stream("GET", cand.url, headers=cand.headers) as r:
            if r.status_code != 200:
                return None, r.status_code, f"HTTP {r.status_code}"
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    return None, r.status_code, "file too large"
            data = bytes(buf)
    except httpx.HTTPError as e:
        return None, None, f"{type(e).__name__}: {e}"
    if not _looks_valid(cand.kind, data, cand.full_text_marker):
        return None, 200, f"not a valid full-text {cand.kind} (got {data[:40]!r})"
    return data, 200, "ok"


def _is_the_article(data: bytes, title: str | None) -> bool:
    """False only when the file has readable text and the paper's title clearly isn't in it."""
    if not title:
        return True
    from .citations import title_match
    from .inbox import _first_pages_text
    text = _first_pages_text(data)
    return len(text.strip()) < 200 or title_match(clean_text(title), text) >= 0.5


def _log_attempt(conn, doi, source, url, outcome, http_status=None, detail=None):
    conn.execute(
        "INSERT INTO fetch_attempts (doi, source, url, outcome, http_status, detail) VALUES (%s,%s,%s,%s,%s,%s)",
        (doi, source, url, outcome, http_status, (detail or "")[:1000]),
    )


def clean_text(t: str | None) -> str | None:
    """'Impact of <scp>pH</scp> &amp; temperature' -> 'Impact of pH & temperature'"""
    if not t:
        return t
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t))).strip()


def _save_metadata(conn, doi: str, lk: Lookup) -> None:
    m = dict(lk.meta, title=clean_text(lk.meta.get("title")), journal=clean_text(lk.meta.get("journal")))
    abs_src = next((src for src in ABSTRACT_PRIORITY if src in lk.abstracts), None)
    conn.execute(
        """UPDATE papers SET title=COALESCE(%s, title), year=COALESCE(%s, year),
               journal=COALESCE(%s, journal), publisher=COALESCE(%s, publisher),
               authors=COALESCE(%s, authors), is_retracted=COALESCE(%s, is_retracted),
               is_oa=COALESCE(%s, is_oa), oa_status=COALESCE(%s, oa_status),
               license=COALESCE(%s, license), pmcid=COALESCE(%s, pmcid), pmid=COALESCE(%s, pmid),
               abstract=COALESCE(%s, abstract), abstract_source=COALESCE(%s, abstract_source),
               metadata=%s, attempts=attempts+1, updated_at=now()
           WHERE doi=%s""",
        (m.get("title"), m.get("year"), m.get("journal"), m.get("publisher"),
         json.dumps(m.get("authors")) if m.get("authors") else None,
         m.get("is_retracted"), m.get("is_oa"), m.get("oa_status"), m.get("license"),
         m.get("pmcid"), m.get("pmid"), lk.abstracts.get(abs_src) if abs_src else None, abs_src,
         json.dumps(lk.raw, default=str), doi),
    )


def enrich(limit: int | None = None) -> dict[str, int]:
    """Refresh metadata + abstracts (no downloads) for papers that have no abstract yet."""
    with db.connect() as conn:
        dois = [r["doi"] for r in conn.execute(
            "SELECT doi FROM papers WHERE abstract IS NULL AND status <> 'invalid_doi' ORDER BY doi LIMIT %s", (limit,)
        ).fetchall()]
    got = 0
    with db.connect() as conn, _http() as c:
        for d in dois:
            lk = discover(c, d)
            _save_metadata(conn, d, lk)
            got += bool(lk.abstracts)
            log.info("%-10s %s", "abstract" if lk.abstracts else "no abstract", d)
    return {"checked": len(dois), "abstract_found": got}


def process(doi: str) -> str:
    """Fetch one DOI end to end. Returns the final status."""
    with db.connect() as conn, _http() as c:
        lk = discover(c, doi)
        _save_metadata(conn, doi, lk)

        if not lk.found_in_any_index:
            _log_attempt(conn, doi, "all", None, "no_candidate", detail="DOI not found in Crossref or any index")
            _set_status(conn, doi, "invalid_doi", "DOI not found in Crossref or any index")
            return "invalid_doi"

        if not lk.candidates:
            _log_attempt(conn, doi, "all", None, "no_candidate", detail="no legal open copy listed by any source")
            _set_status(conn, doi, "not_available", "no legal open copy found")
            return "not_available"

        last_err, blocked_url = "", None
        for cand in lk.candidates:
            data, code, detail = _download(c, cand)
            if data is not None and not _is_the_article(data, lk.meta.get("title")):
                # e.g. a supplementary table PDF sitting next to the article in the PMC dataset
                data, code, detail = None, 200, "file is not the article (title not found in its text)"
            if data is None:
                outcome = "not_pdf" if code == 200 else "http_error"
                _log_attempt(conn, doi, cand.source, cand.url, outcome, code, detail)
                last_err = f"{cand.source}: {detail}"
                # 401/403/HTML page = the copy exists but blocks automated access; a person can get it.
                if blocked_url is None and cand.kind == "pdf" and code in (200, 401, 403):
                    blocked_url = cand.url
                continue

            key = s3_key(doi, cand.kind)
            ctype = "application/pdf" if cand.kind == "pdf" else "application/xml"
            digest = storage.sha256(data)
            storage.put(key, data, ctype, {"doi": doi, "sha256": digest, "source-url": cand.url[:1000]})
            conn.execute(
                """UPDATE papers SET status='downloaded', source=%s, source_url=%s, s3_bucket=%s, s3_key=%s,
                       sha256=%s, file_size=%s, content_type=%s, license=COALESCE(%s, license),
                       last_error=NULL, manual_url=NULL, retrieved_at=now(), updated_at=now()
                   WHERE doi=%s""",
                (cand.source, cand.url, settings.s3_bucket, key, digest, len(data), ctype, cand.license, doi),
            )
            _log_attempt(conn, doi, cand.source, cand.url, "success", code, f"{len(data)} bytes")
            return "downloaded"

        if blocked_url:
            conn.execute(
                "UPDATE papers SET status='pending_manual', manual_url=%s, last_error=%s, updated_at=now() WHERE doi=%s",
                (blocked_url, f"free copy exists but blocks automated download ({last_err})", doi),
            )
            return "pending_manual"
        _set_status(conn, doi, "download_failed", last_err)
        return "download_failed"


def attach_file(doi: str, path: str, source: str = "manual", note: str | None = None) -> dict:
    """Store a file a person downloaded by hand (pending_manual / gap list) with full provenance."""
    from pathlib import Path

    data = Path(path).read_bytes()
    kind = "pdf" if b"%PDF" in data[:1024] else "xml" if _looks_valid("xml", data) else None
    if kind is None:
        raise ValueError(f"{path} is neither a PDF nor JATS XML")
    key, digest = s3_key(doi, kind), storage.sha256(data)
    ctype = "application/pdf" if kind == "pdf" else "application/xml"
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM papers WHERE doi=%s", (doi,)).fetchone():
            db.register_dois(conn, [doi])
        storage.put(key, data, ctype, {"doi": doi, "sha256": digest, "source-url": f"{source}-upload"})
        conn.execute(
            """UPDATE papers SET status='downloaded', source=%s, source_url=COALESCE(manual_url, %s),
                   s3_bucket=%s, s3_key=%s, sha256=%s, file_size=%s, content_type=%s,
                   last_error=NULL, retrieved_at=now(), updated_at=now()
               WHERE doi=%s""",
            (source, f"{source}-upload", settings.s3_bucket, key, digest, len(data), ctype, doi),
        )
        _log_attempt(conn, doi, source, Path(path).name, "success", None,
                     f"{len(data)} bytes" + (f"; {note}" if note else ""))
    return {"doi": doi, "s3_key": key, "sha256": digest, "bytes": len(data)}


def _set_status(conn, doi, status, err):
    conn.execute("UPDATE papers SET status=%s, last_error=%s, updated_at=now() WHERE doi=%s", (status, err, doi))


def run(limit: int | None = None, workers: int = 4, retry_failed: bool = False, max_attempts: int = 3,
        recheck_missing: bool = False) -> dict[str, int]:
    """recheck_missing: try every paper we don't have yet again, e.g. after adding a publisher
    TDM token or months later when more papers have become open access."""
    statuses = ["pending"] + (["download_failed"] if retry_failed or recheck_missing else [])
    if recheck_missing:
        statuses += ["not_available", "pending_manual"]
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT doi FROM papers WHERE status = ANY(%s) AND (%s OR attempts < %s)
               ORDER BY created_at, doi LIMIT %s""",
            (statuses, recheck_missing, max_attempts, limit),
        ).fetchall()
    dois = [r["doi"] for r in rows]
    log.info("fetching %d DOIs with %d workers", len(dois), workers)
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process, d): d for d in dois}
        for i, fut in enumerate(as_completed(futures), 1):
            d = futures[fut]
            try:
                st = fut.result()
            except Exception as e:
                log.exception("%s crashed", d)
                with db.connect() as conn:
                    _set_status(conn, d, "download_failed", f"crash: {e}")
                st = "download_failed"
            counts[st] = counts.get(st, 0) + 1
            log.info("[%d/%d] %-15s %s", i, len(dois), st, d)
    return counts
