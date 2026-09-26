"""Web interface + REST API.

    start.bat                      (double-click: starts Docker services and opens the app)
    python -m lemkb ui             (same, from a terminal)

UI at http://127.0.0.1:8000, API under /api (interactive docs at /docs).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import backup, citations, db, fetcher, harvest, inbox, jobs, mediaassist, parse, service, storage
from .config import settings

UI_DIR = Path(__file__).parent / "ui"
MAX_UPLOAD = 150 * 1024 * 1024

app = FastAPI(title="Lemnisca Media Knowledge Base", version="0.2.0",
              description="MediaAssist facts, verified citations and paper text, with provenance on every answer.")
api = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@api.get("/facts")
def facts(component: str | None = None, query_type: str | None = Query(None, enum=service.QUERY_TYPES),
          text: str | None = None, limit: int = Query(50, le=500)):
    return service.search_facts(component, query_type, text, limit)


@api.get("/facts/{fact_id}/evidence")
def fact_evidence(fact_id: int, limit: int = Query(5, le=50)):
    if (r := service.evidence_for_fact(fact_id, limit)) is None:
        raise HTTPException(404, "fact not found")
    return r


@api.get("/components")
def components():
    return service.list_components()


@api.get("/passages/search")
def passages_search(q: str, doi: str | None = None, kind: str | None = None,
                    evidence_level: str | None = None, limit: int = Query(20, le=100)):
    return service.search_passages(q, doi, kind, evidence_level, limit)


@api.get("/papers")
def papers(status: str | None = None, q: str | None = None, limit: int = Query(200, le=2000)):
    with db.connect() as conn:
        return conn.execute(
            """SELECT p.doi, p.title, p.year, p.journal, p.publisher, p.status, p.evidence_level, p.source,
                      p.manual_url, p.is_retracted,
                      (SELECT count(*) FROM row_citations c WHERE c.doi = p.doi) AS facts_citing
               FROM papers p
               WHERE (%(s)s::text IS NULL OR p.status = %(s)s)
                 AND (%(q)s::text IS NULL OR p.title ILIKE %(ql)s OR p.doi ILIKE %(ql)s OR p.journal ILIKE %(ql)s)
               ORDER BY facts_citing DESC, p.doi LIMIT %(limit)s""",
            {"s": status, "q": q, "ql": f"%{q}%" if q else None, "limit": limit},
        ).fetchall()


@api.get("/gaps")
def gaps(limit: int = Query(100, le=2000)):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM gap_priority LIMIT %s", (limit,)).fetchall()
    return [dict(r, doi_url=f"https://doi.org/{r['doi']}") for r in rows]


@api.get("/gaps.csv")
def gaps_csv():
    """Every paper we don't have full text for: the list to download by hand."""
    import csv
    import io
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT g.facts_citing, g.doi, 'https://doi.org/' || g.doi AS doi_url, g.title, g.journal, g.publisher,
                      g.year, g.status, g.evidence_level, p.manual_url, p.last_error AS reason
               FROM gap_priority g JOIN papers p USING (doi)"""
        ).fetchall()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["facts_citing", "doi", "doi_url", "title", "journal", "publisher", "year",
                                        "status", "evidence_level", "manual_url", "reason"])
    w.writeheader()
    w.writerows(rows)
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv",  # BOM so Excel opens UTF-8 correctly
                    headers={"Content-Disposition": 'attachment; filename="missing_papers.csv"'})


@api.get("/access/users")
def access_users(include_revoked: bool = False):
    from . import mcp_http
    return mcp_http.list_users(include_revoked)


@api.post("/access/users")
def access_add(name: str):
    from . import mcp_http
    try:
        return {"name": name.strip(), "token": mcp_http.add_user(name)}
    except ValueError as e:
        raise HTTPException(422, str(e))


@api.delete("/access/users/{name}")
def access_revoke(name: str):
    from . import mcp_http
    if not mcp_http.revoke_user(name):
        raise HTTPException(404, "no active user with that name")
    return {"revoked": name}


@api.get("/access/info")
def access_info():
    """Connection details shown on the Access page."""
    import os
    return {"mcp_url": os.environ.get("MCP_PUBLIC_URL") or "https://13-126-230-191.sslip.io/mcp"}


@api.get("/coverage")
def coverage():
    return service.coverage()


# DOIs contain "/", so they're matched as paths; the most specific routes come first.
@api.get("/papers/{doi:path}/passages")
def paper_passages(doi: str, section: str | None = None, start: int = 0, limit: int = Query(500, le=2000)):
    return service.get_passages(unquote(doi), section, start, limit)


@api.get("/papers/{doi:path}/file")
def paper_file(doi: str):
    p = service.get_paper(unquote(doi), with_file_link=False)
    if not p or not p["s3_key"]:
        raise HTTPException(404, "no file stored for this paper")
    body = storage.client().get_object(Bucket=settings.s3_bucket, Key=p["s3_key"])["Body"].read()
    ext = "pdf" if p["content_type"] == "application/pdf" else "xml"
    name = p["doi"].replace("/", "_") + f".{ext}"
    return Response(body, media_type=p["content_type"], headers={"Content-Disposition": f'inline; filename="{name}"'})


@api.post("/papers/{doi:path}/upload")
async def paper_upload(doi: str, request: Request, filename: str = "upload.pdf", force: bool = False):
    """Attach a PDF to one specific paper. The file must match the paper's title unless force=true."""
    doi = unquote(doi).lower()
    data = await _read_body(request)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / Path(filename).name
        path.write_bytes(data)
        with db.connect() as conn:
            if not conn.execute("SELECT 1 FROM papers WHERE doi=%s", (doi,)).fetchone():
                raise HTTPException(404, "paper not in catalog")
            match = inbox.identify(conn, path, data)
        if match.get("doi") != doi and not force:
            found = f"it looks like {match['doi']}" if match.get("doi") else match.get("reason", "no match")
            raise HTTPException(409, f"This file doesn't look like {doi} ({found}). Check the file, or upload anyway.")
        stored = fetcher.attach_file(doi, str(path), source="ui-upload",
                                     note="uploaded in UI" + (" (forced, title not verified)" if match.get("doi") != doi else ""))
    parse.parse(doi=doi)
    return {"stored": stored, "verified": match.get("doi") == doi}


@api.post("/inbox")
async def inbox_upload(request: Request, filename: str = "upload.pdf"):
    """Drop any PDF: it is identified (DOI in name/text, or title), verified and stored."""
    data = await _read_body(request)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / Path(filename).name
        path.write_bytes(data)
        res = inbox.process(tmp)
    for r in res:
        if r.get("result") == "stored":
            parse.parse(doi=r["doi"])
    return res[0] if res else {"result": "unmatched", "reason": "not a PDF/XML file"}


@api.get("/papers/{doi:path}")
def paper(doi: str):
    if (p := service.get_paper(unquote(doi), with_file_link=False)) is None:
        raise HTTPException(404, "paper not in catalog")
    return p


# ---------------------------------------------------------------------------
# Jobs: import / fetch / parse / backup
# ---------------------------------------------------------------------------

async def _read_body(request: Request) -> bytes:
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large")
    return data


STAGING = Path(tempfile.gettempdir()) / "lemkb_staging"


@api.post("/import/stage")
async def import_stage(request: Request, filename: str, query_type: str | None = None):
    """Upload one MediaAssist export; it is checked and staged, then /import/run processes all staged files."""
    data = await _read_body(request)
    inferred = mediaassist.infer_type(filename)
    qtype = query_type or (inferred[0] if inferred else None)
    if not qtype:
        raise HTTPException(422, f"Can't tell the query type from '{filename}'. Choose one.")
    STAGING.mkdir(exist_ok=True)
    (STAGING / f"{qtype}__{Path(filename).name}").write_bytes(data)
    return {"filename": filename, "query_type": qtype}


@api.post("/import/run")
def import_run(fetch: bool = True, parse_text: bool = True):
    staged = sorted(STAGING.glob("*__*")) if STAGING.exists() else []
    if not staged:
        raise HTTPException(400, "no files staged")

    def ingest_all():
        out = []
        for f in staged:
            qtype, name = f.name.split("__", 1)
            inferred = mediaassist.infer_type(name)
            params = inferred[1] if inferred and inferred[0] == qtype else {}
            out.append({"file": name, "query_type": qtype,
                        **mediaassist.ingest_bytes(f.read_bytes(), Path(name).suffix, qtype, params, "file", name)})
            f.unlink()
        return out

    steps = [("Import files", ingest_all), ("Verify citations", citations.resolve)]
    if fetch:
        steps.append(("Download papers", lambda: fetcher.run(workers=4, retry_failed=True)))
    if parse_text:
        steps.append(("Extract text", parse.parse))
    return _start("import", steps)


@api.post("/jobs/fetch")
def job_fetch(recheck_missing: bool = False):
    return _start("fetch", [("Download papers", lambda: fetcher.run(workers=4, retry_failed=True,
                                                                     recheck_missing=recheck_missing)),
                            ("Extract text", parse.parse)])


@api.post("/jobs/harvest")
def job_harvest():
    """Download every MediaAssist export, then verify citations, fetch papers and extract text."""
    return _start("harvest", [("Harvest MediaAssist", harvest.run), ("Verify citations", citations.resolve),
                              ("Download papers", lambda: fetcher.run(workers=4, retry_failed=True)),
                              ("Extract text", parse.parse)])


@api.post("/jobs/parse")
def job_parse():
    return _start("parse", [("Extract text", parse.parse)])


@api.post("/jobs/enrich")
def job_enrich():
    return _start("enrich", [("Fetch abstracts", fetcher.enrich), ("Extract text", parse.parse)])


@api.post("/jobs/backup")
def job_backup():
    return _start("backup", [("Backup", backup.run)])


@api.get("/jobs/current")
def job_current():
    return jobs.current() or {}


@api.get("/jobs/{job_id}")
def job_get(job_id: str):
    if (j := jobs.get(job_id)) is None:
        raise HTTPException(404, "job not found")
    return j


def _start(kind, steps):
    try:
        return jobs.start(kind, steps)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


app.include_router(api)
app.mount("/static", StaticFiles(directory=UI_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(UI_DIR / "index.html")
