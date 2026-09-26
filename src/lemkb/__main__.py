"""lemkb command line.

    python -m lemkb check                          verify Postgres + S3 are reachable
    python -m lemkb migrate                        apply db/init/*.sql (idempotent)
    python -m lemkb ingest-dir FOLDER              load every MediaAssist export in a folder (type from filename)
    python -m lemkb resolve                        verify each row's citation -> DOI, register papers
    python -m lemkb enrich                         backfill abstracts/metadata for papers without one
    python -m lemkb load-dois FILE [--column doi]  register DOIs from a CSV/TXT
    python -m lemkb ingest FILE --type solubility  load a MediaAssist table downloaded by hand
    python -m lemkb harvest CONFIG [--dry-run]     automated MediaAssist download (needs permission)
    python -m lemkb fetch [--limit N] [--workers 4] [--retry-failed]
    python -m lemkb report                         coverage + gap list
    python -m lemkb get DOI                        metadata + temporary PDF link
    python -m lemkb upload DOI FILE                attach a PDF a person downloaded by hand
    python -m lemkb inbox [FOLDER]                 match + store every PDF dropped in data/inbox
    python -m lemkb parse [--doi D] [--force]      papers -> citable passages (Layer 3)
    python -m lemkb ui                             web interface (opens the browser); same as start.bat
    python -m lemkb serve-api [--port 8000]        REST API only (docs at /docs)
    python -m lemkb serve-mcp [--http]             MCP server for AI agents (stdio by default)
    python -m lemkb backup [--out DIR]             Postgres dump + all S3 files + verified manifest
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from . import backup, citations, db, fetcher, harvest, inbox, mediaassist, parse, storage
from .config import settings
from .doi import find_all, normalize


def cmd_check(_):
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM papers").fetchone()["n"]
        print(f"postgres ok  ({n} papers)")
    storage.client().head_bucket(Bucket=settings.s3_bucket)
    print(f"s3 ok        (bucket '{settings.s3_bucket}' at {settings.s3_endpoint or 'AWS'})")
    print(f"contact email: {settings.contact_email or 'NOT SET - Unpaywall will be skipped'}")


def cmd_migrate(_):
    files = sorted((Path(__file__).resolve().parents[2] / "db" / "init").glob("*.sql"))
    with db.connect() as conn:
        for f in files:
            conn.execute(f.read_text(encoding="utf-8"))
            print(f"applied {f.name}")


def cmd_ingest_dir(a):
    for r in mediaassist.ingest_dir(a.folder):
        print(json.dumps(r, ensure_ascii=False))
    if not a.no_resolve:
        print("resolving citations...")
        print(json.dumps(citations.resolve(), indent=2))


def cmd_resolve(a):
    print(json.dumps(citations.resolve(only_unresolved=not a.all, problems_only=a.problems), indent=2))


def cmd_enrich(a):
    print(json.dumps(fetcher.enrich(limit=a.limit), indent=2))


def cmd_load_dois(a):
    path = Path(a.file)
    if path.suffix.lower() == ".csv" and a.column:
        import pandas as pd
        raw = pd.read_csv(path, dtype=str)[a.column].dropna().tolist()
        dois = [d for d in map(normalize, raw) if d]
    else:
        dois = find_all(path.read_text(encoding="utf-8", errors="replace"))
    with db.connect() as conn:
        new = db.register_dois(conn, dois)
    print(f"{len(set(dois))} DOIs found, {new} new")


def cmd_ingest(a):
    params = json.loads(a.params) if a.params else None
    print(json.dumps(mediaassist.ingest_file(a.file, a.type, params), indent=2))
    if not a.no_resolve:
        print(json.dumps(citations.resolve(), indent=2))


def cmd_harvest_all(a):
    res = harvest.run(query_types=a.types.split(",") if a.types else None,
                      components=a.components.split(",") if a.components else None,
                      delay=a.delay, dry_run=a.dry_run)
    print(json.dumps(res, indent=2))


def cmd_harvest(a):
    res = mediaassist.harvest(a.config, delay=a.delay, dry_run=a.dry_run)
    print(json.dumps(res, indent=2, default=str))


def cmd_fetch(a):
    counts = fetcher.run(limit=a.limit, workers=a.workers, retry_failed=a.retry_failed,
                         recheck_missing=a.recheck_missing)
    print(json.dumps(counts, indent=2))


def cmd_report(a):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM coverage_report").fetchall()
        total = sum(r["papers"] for r in rows)
        print(f"\nPapers: {total}")
        for r in rows:
            print(f"  {r['status']:<16} {r['papers']:>6}  {r['pct']:>5}%")
        src = conn.execute(
            "SELECT source, count(*) n FROM papers WHERE status='downloaded' GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        if src:
            print("\nDownloaded via:")
            for r in src:
                print(f"  {r['source']:<16} {r['n']:>6}")
        ev = conn.execute("SELECT evidence_level, count(*) n FROM papers GROUP BY 1 ORDER BY 2 DESC").fetchall()
        if ev:
            print("\nWhat agents can read (evidence level):")
            for r in ev:
                print(f"  {r['evidence_level']:<16} {r['n']:>6}")
        facts = conn.execute(
            """SELECT r.query_type, count(*) n,
                      count(*) FILTER (WHERE c.verified) verified,
                      count(*) FILTER (WHERE c.source_kind = 'non_doi') non_doi,
                      count(*) FILTER (WHERE c.issue IS NOT NULL) issues,
                      count(*) FILTER (WHERE p.status = 'downloaded') full_text
               FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id
               LEFT JOIN papers p ON p.doi = c.doi GROUP BY 1 ORDER BY 1"""
        ).fetchall()
        if facts:
            print("\nMediaAssist rows        rows  verified-DOI  non-DOI-source  flagged  full-text")
            for r in facts:
                print(f"  {r['query_type']:<18} {r['n']:>6} {r['verified']:>13} {r['non_doi']:>15}"
                      f" {r['issues']:>8} {r['full_text']:>10}")
        if a.gaps:
            gaps = conn.execute(
                """SELECT g.facts_citing, g.doi, g.status, g.evidence_level, g.manual_url, g.publisher,
                          g.journal, g.year, g.title, p.last_error
                   FROM gap_priority g JOIN papers p USING (doi)"""
            ).fetchall()
            import csv
            with open(a.gaps, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(gaps[0].keys()) if gaps else ["doi"])
                w.writeheader()
                w.writerows(gaps)
            print(f"\nGap list ({len(gaps)} papers, most-cited first) written to {a.gaps}")


def cmd_upload(a):
    print(json.dumps(fetcher.attach_file(normalize(a.doi), a.file), indent=2))


def cmd_inbox(a):
    Path(a.folder).mkdir(parents=True, exist_ok=True)
    res = inbox.process(a.folder, replace=a.replace)
    for r in res:
        print(json.dumps(r))
    print(f"{sum(r['result'] == 'stored' for r in res)} stored, "
          f"{sum(r['result'] == 'unmatched' for r in res)} unmatched (left in {a.folder})")


def cmd_parse(a):
    print(json.dumps(parse.parse(doi=normalize(a.doi) if a.doi else None, force=a.force), indent=2))


def cmd_backup(a):
    print(json.dumps(backup.run(a.out) if a.out else backup.run(), indent=2))


def cmd_serve_api(a):
    import uvicorn
    uvicorn.run("lemkb.api:app", host=a.host, port=a.port)


def cmd_ui(a):
    import threading
    import uvicorn
    import webbrowser
    url = f"http://127.0.0.1:{a.port}"
    if not a.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Lemnisca Media KB running at {url}  (close this window to stop)")
    uvicorn.run("lemkb.api:app", host="127.0.0.1", port=a.port, log_level="warning")


def cmd_serve_mcp(a):
    if a.http:
        from . import mcp_http  # token-protected; refuses to start without tokens
        mcp_http.serve(host=a.host, port=a.port, public_host=a.public_host)
    else:
        from . import mcp_server
        mcp_server.server.run("stdio")


def cmd_mcp_token(a):
    from . import mcp_http
    if a.action == "add":
        token = mcp_http.add_user(a.name)
        print(f"Token for {a.name} (shown once; send it to them privately):")
        print(token)
    elif a.action == "revoke":
        print("revoked" if mcp_http.revoke_user(a.name) else "no active user with that name")
    else:
        for u in mcp_http.list_users():
            print(f"{u['name']:20} created {u['created_at']:%Y-%m-%d}  last used {u['last_used_at'] or 'never'}")


def cmd_get(a):
    doi = normalize(a.doi)
    with db.connect() as conn:
        p = conn.execute(
            "SELECT doi, title, year, journal, status, source, source_url, s3_key, sha256, license, is_retracted, last_error "
            "FROM papers WHERE doi=%s", (doi,)
        ).fetchone()
        if not p:
            sys.exit(f"{doi} not in catalog")
        facts = conn.execute("SELECT count(*) n FROM fact_evidence WHERE doi=%s", (doi,)).fetchone()["n"]
    p = dict(p, mediaassist_rows=facts)
    if p["s3_key"]:
        p["download_url"] = storage.presigned_url(p["s3_key"])
    print(json.dumps(p, indent=2, default=str))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="lemkb")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check").set_defaults(fn=cmd_check)

    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)

    s = sub.add_parser("ingest-dir"); s.add_argument("folder"); s.add_argument("--no-resolve", action="store_true")
    s.set_defaults(fn=cmd_ingest_dir)

    s = sub.add_parser("resolve"); s.add_argument("--all", action="store_true", help="re-resolve every row")
    s.add_argument("--problems", action="store_true", help="re-resolve rows with unverified / DOI-less citations")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("enrich"); s.add_argument("--limit", type=int); s.set_defaults(fn=cmd_enrich)

    s = sub.add_parser("load-dois"); s.add_argument("file"); s.add_argument("--column")
    s.set_defaults(fn=cmd_load_dois)

    s = sub.add_parser("ingest"); s.add_argument("file"); s.add_argument("--type", required=True)
    s.add_argument("--params", help='JSON, e.g. \'{"component":"glucose"}\''); s.add_argument("--no-resolve", action="store_true")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("harvest-all", help="download every MediaAssist export (all components, all types)")
    s.add_argument("--types", help="comma list, e.g. solubility,stability"); s.add_argument("--components", help="comma list of names")
    s.add_argument("--delay", type=float, default=harvest.DELAY); s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_harvest_all)

    def cmd_tidy(_):
        from . import layout
        print(json.dumps(layout.tidy(), indent=2))
    def cmd_s3(action):
        def run(_):
            from . import s3_migrate
            print(json.dumps(getattr(s3_migrate, action)(), indent=2, default=str))
        return run
    sub.add_parser("s3-check", help="test access to the AWS bucket in .env (AWS_S3_*)").set_defaults(fn=cmd_s3("check"))
    sub.add_parser("s3-migrate", help="copy every file from MinIO to the AWS bucket, verified").set_defaults(fn=cmd_s3("migrate"))

    def cmd_db(action):
        def run(a):
            from . import db_migrate
            fn = getattr(db_migrate, action)
            kw = {"force": a.force} if action == "migrate" else {"to_local": a.local} if action == "switch" else {}
            print(json.dumps(fn(**kw), indent=2, default=str))
        return run
    sub.add_parser("db-check", help="connect to the RDS database in .env (RDS_*)").set_defaults(fn=cmd_db("check"))
    s = sub.add_parser("db-migrate", help="copy the local database to RDS and verify every table")
    s.add_argument("--force", action="store_true", help="overwrite a target that already has tables")
    s.set_defaults(fn=cmd_db("migrate"))
    s = sub.add_parser("db-switch", help="point the app at RDS (rewrites POSTGRES_* in .env)")
    s.add_argument("--local", action="store_true", help="switch back to the local container")
    s.set_defaults(fn=cmd_db("switch"))

    def cmd_validate(a):
        from . import validate
        print(json.dumps(validate.run(n_per_type=a.per_type, n_full_text=a.full_text, seed=a.seed), indent=2, default=str))
    s = sub.add_parser("validate", help="spot-check random facts end to end (MediaAssist, DOI, S3, file, query path)")
    s.add_argument("--per-type", type=int, default=4); s.add_argument("--full-text", type=int, default=10)
    s.add_argument("--seed", type=int, default=42); s.set_defaults(fn=cmd_validate)

    sub.add_parser("tidy-layout", help="move S3 files into the fixed layout (papers/, parsed/, mediaassist/raw/)").set_defaults(fn=cmd_tidy)

    s = sub.add_parser("harvest"); s.add_argument("config"); s.add_argument("--delay", type=float, default=1.0)
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_harvest)

    s = sub.add_parser("fetch"); s.add_argument("--limit", type=int); s.add_argument("--workers", type=int, default=4)
    s.add_argument("--retry-failed", action="store_true")
    s.add_argument("--recheck-missing", action="store_true", help="retry not_available/pending_manual too")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("report"); s.add_argument("--gaps", help="write gap list CSV to this path")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("get"); s.add_argument("doi"); s.set_defaults(fn=cmd_get)

    s = sub.add_parser("upload"); s.add_argument("doi"); s.add_argument("file"); s.set_defaults(fn=cmd_upload)

    s = sub.add_parser("parse"); s.add_argument("--doi"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_parse)

    s = sub.add_parser("backup"); s.add_argument("--out"); s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("serve-api"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve_api)

    s = sub.add_parser("ui"); s.add_argument("--port", type=int, default=8000); s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_ui)

    s = sub.add_parser("serve-mcp"); s.add_argument("--http", action="store_true"); s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--public-host", help="public DNS name when hosted")
    s.set_defaults(fn=cmd_serve_mcp)

    s = sub.add_parser("mcp-token", help="manage access tokens for the hosted MCP server")
    s.add_argument("action", choices=["add", "list", "revoke"]); s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_mcp_token)

    s = sub.add_parser("inbox"); s.add_argument("folder", nargs="?", default="data/inbox")
    s.add_argument("--replace", action="store_true", help="replace papers we already have")
    s.set_defaults(fn=cmd_inbox)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
