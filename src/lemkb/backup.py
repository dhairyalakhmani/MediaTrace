"""Point-in-time backup: Postgres dump + every S3 object + a manifest to verify against.

    backups/<timestamp>/
        postgres.dump      pg_dump custom format (restore with pg_restore)
        s3/<key>           every object in the bucket, same keys
        manifest.json      counts + sha256 of every paper file, checked after copying
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import db, storage
from .config import settings

ROOT = Path(__file__).resolve().parents[2]


def pg_tool(tool: str, pg: dict, args: list[str], stdin=None, stdout=None) -> subprocess.CompletedProcess:
    """Run pg_dump / pg_restore / psql (PostgreSQL 18 tools) against any server.

    The password goes in via the environment (PGPASSWORD), never on the command line.
    """
    # Runs in a throwaway PostgreSQL 18 container on the compose network, where the
    # local database is reachable as "postgres" (its service name), not "localhost".
    host = "postgres" if pg["host"] in ("localhost", "127.0.0.1") else pg["host"]
    port = "5432" if host == "postgres" else str(pg["port"])
    env_args = ["-e", "PGPASSWORD", "-e", f"PGSSLMODE={pg.get('sslmode') or 'prefer'}"]
    cmd = ["docker", "compose", "run", "--rm", "-T", "--no-deps", *env_args, "pgtools", tool,
           "-h", host, "-p", port, "-U", pg["user"], "-d", pg["dbname"], *args]
    import os
    return subprocess.run(cmd, cwd=ROOT, stdin=stdin, stdout=stdout, check=True,
                          env={**os.environ, "PGPASSWORD": pg["password"] or ""})


def run(out_dir: str | Path = ROOT / "backups") -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(out_dir) / stamp
    (target / "s3").mkdir(parents=True)

    # 1. Postgres (local or RDS), dumped with the container's pg_dump so none is needed locally.
    with open(target / "postgres.dump", "wb") as f:
        pg_tool("pg_dump", settings.pg, ["-Fc"], stdout=f)

    # 2. Every object in the bucket.
    s3 = storage.client()
    n_obj = n_bytes = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
        for obj in page.get("Contents", []):
            dest = target / "s3" / obj["Key"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(settings.s3_bucket, obj["Key"], str(dest))
            n_obj += 1
            n_bytes += obj["Size"]

    # 3. Verify paper files against the catalog's hashes.
    with db.connect() as conn:
        papers = conn.execute("SELECT doi, s3_key, sha256 FROM papers WHERE s3_key IS NOT NULL").fetchall()
        counts = {t: conn.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"]
                  for t in ("papers", "mediaassist_rows", "row_citations", "passages")}
    bad = [p["doi"] for p in papers
           if hashlib.sha256((target / "s3" / p["s3_key"]).read_bytes()).hexdigest() != p["sha256"]]
    manifest = {"created_at": stamp, "bucket": settings.s3_bucket, "s3_objects": n_obj, "s3_bytes": n_bytes,
                "postgres_dump_bytes": (target / "postgres.dump").stat().st_size, "table_rows": counts,
                "paper_files_verified": len(papers) - len(bad), "paper_files_mismatched": bad}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return {"path": str(target), **manifest}
