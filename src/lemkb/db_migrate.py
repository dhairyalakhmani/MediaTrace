"""Move the Postgres database from the local container to AWS RDS (or any Postgres).

Target connection comes from .env: RDS_HOST, RDS_PORT, RDS_DB, RDS_USER, RDS_PASSWORD
(optional RDS_SSLMODE, default "require": RDS connections are encrypted).

    lemkb db-check      connect to the target, show its version and whether it's empty
    lemkb db-migrate    dump local -> restore to target -> compare every table
    lemkb db-switch     point the app at the target (rewrites POSTGRES_* in .env); --local to go back

The local database is never modified, so switching back is always possible.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from .backup import ROOT, pg_tool
from .config import pg_params, settings

TABLES = ["papers", "fetch_attempts", "mediaassist_downloads", "mediaassist_rows", "row_citations",
          "paper_texts", "passages"]
# content fingerprints: if these match, the data is identical, not just the row counts
FINGERPRINTS = {
    "papers": "SELECT md5(string_agg(doi || '|' || status || '|' || coalesce(sha256, '') || '|' || coalesce(s3_key, ''), ',' ORDER BY doi)) FROM papers",
    "mediaassist_rows": "SELECT md5(string_agg(id || '|' || md5(data::text), ',' ORDER BY id)) FROM mediaassist_rows",
    "row_citations": "SELECT md5(string_agg(row_id || '|' || coalesce(doi, '') || '|' || verified, ',' ORDER BY row_id)) FROM row_citations",
    "passages": "SELECT md5(string_agg(doi || '|' || seq || '|' || md5(text), ',' ORDER BY doi, seq)) FROM passages",
}
VIEWS = ["fact_evidence", "gap_priority", "coverage_report", "passage_index", "v_uptake_rate", "v_codependency"]


def target() -> dict:
    t = pg_params("RDS", {"sslmode": "require"})
    missing = [k for k in ("host", "dbname", "user", "password") if not t.get(k)]
    if missing:
        raise SystemExit(f"Set RDS_{', RDS_'.join(m.upper().replace('DBNAME', 'DB') for m in missing)} in .env first.")
    return t


def _connect(pg: dict):
    return psycopg.connect(make_conninfo(**pg), row_factory=dict_row, autocommit=True, connect_timeout=15)


def _tables(conn) -> list[str]:
    return [r["tablename"] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")]


def check() -> dict:
    t = target()
    with _connect(t) as conn:
        return {"server": conn.execute("SELECT version() v").fetchone()["v"].split(",")[0],
                "host": t["host"], "database": t["dbname"], "ssl": conn.info.pgconn.ssl_in_use,
                "existing_tables": _tables(conn)}


def _snapshot(conn) -> dict:
    snap = {f"rows:{t}": conn.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"] for t in TABLES}
    snap.update({f"md5:{t}": conn.execute(q).fetchone()["md5"] for t, q in FINGERPRINTS.items()})
    snap.update({f"view:{v}": conn.execute(f"SELECT count(*) n FROM {v}").fetchone()["n"] for v in VIEWS})
    return snap


def migrate(force: bool = False) -> dict:
    t = target()
    with _connect(t) as conn:
        existing = _tables(conn)
    if existing and not force:
        raise SystemExit(f"Target database already has tables {existing}. Use --force only if you mean to overwrite it.")

    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "lemkb.dump"
        with open(dump, "wb") as f:
            pg_tool("pg_dump", settings.pg, ["-Fc"], stdout=f)
        with open(dump, "rb") as f:
            extra = ["--clean", "--if-exists"] if force else []
            pg_tool("pg_restore", t, ["--no-owner", "--no-privileges", "--exit-on-error", *extra], stdin=f)
        size = dump.stat().st_size

    with psycopg.connect(settings.pg_dsn, row_factory=dict_row, autocommit=True) as src, _connect(t) as dst:
        a, b = _snapshot(src), _snapshot(dst)
    diff = {k: {"local": a[k], "target": b.get(k)} for k in a if a[k] != b.get(k)}
    return {"dump_bytes": size, "checks": len(a), "identical": not diff, "differences": diff,
            "summary": {k.split(":", 1)[1]: v for k, v in b.items() if k.startswith("rows:")}}


def switch(to_local: bool = False) -> dict:
    """Rewrite the POSTGRES_* lines in .env (values are never printed)."""
    env_path = ROOT / ".env"
    text = env_path.read_text(encoding="utf-8")
    src = ({"host": "localhost", "port": "5432", "dbname": "lemkb", "user": "lemkb", "password": "lemkb",
            "sslmode": "prefer"} if to_local else target())
    values = {"POSTGRES_HOST": src["host"], "POSTGRES_PORT": str(src["port"]), "POSTGRES_DB": src["dbname"],
              "POSTGRES_USER": src["user"], "POSTGRES_PASSWORD": src["password"], "POSTGRES_SSLMODE": src["sslmode"]}
    for k, v in values.items():
        line = f"{k}={v}"
        if re.search(rf"^{k}=.*$", text, flags=re.M):
            text = re.sub(rf"^{k}=.*$", lambda _m: line, text, flags=re.M)
        else:
            text = text.replace("POSTGRES_PASSWORD=", f"{line}\nPOSTGRES_PASSWORD=", 1) if k != "POSTGRES_PASSWORD" else text + f"\n{line}\n"
    env_path.write_text(text, encoding="utf-8")
    return {"now_using": "local container" if to_local else f"{src['host']} / {src['dbname']}",
            "note": "restart the app (close its window, double-click start.bat) to pick this up"}
