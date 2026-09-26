"""Automation A: MediaAssist tables -> S3 (raw proof) + Postgres (rows) + papers (DOIs).

Two entry points share one ingest path:
  ingest_file()  a table downloaded by hand from the site (works today, no config needed)
  harvest()      replays the site's own Download request for every query in a config file

NOTE: MediaAssist is licensed for academic, non-commercial use. Commercial use requires
permission from CSIR-NCL (director@ncl.res.in). Run harvest() only once that is in place.
"""
from __future__ import annotations

import io
import itertools
import json
import logging
import re
import time
from pathlib import Path

import httpx
import pandas as pd

from . import db, storage
from .config import settings
from .doi import find_all

log = logging.getLogger("lemkb.mediaassist")

_EXT_TYPES = {
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel", ".html": "text/html", ".htm": "text/html",
}


def _parse(data: bytes, ext: str) -> pd.DataFrame:
    ext = ext.lower()
    if ext in (".xlsx", ".xls"):
        frames = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str)
        return pd.concat(frames.values(), ignore_index=True)
    if ext in (".html", ".htm"):
        return pd.concat(pd.read_html(io.BytesIO(data)), ignore_index=True).astype(str)
    text = data.decode("utf-8-sig", errors="replace")
    sep = "\t" if ext == ".tsv" or text.count("\t") > text.count(",") else ","
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)


def _slug(v) -> str:
    return re.sub(r"[^A-Za-z0-9.+-]+", "-", str(v)).strip("-")[:60] or "x"


# option keys that are ids or already covered by the component name
_ID_KEYS = {"component", "selection", "component_id", "component_id_conc", "component_1_id", "component_2_id"}


def s3_key_for(query_type: str, params: dict | None, digest: str, ext: str, original_name: str | None = None) -> str:
    """Browsable key: mediaassist/raw/<type>/<Component>/<type>_<Component>[_<partner/options>]__<hash12>.<ext>

    e.g. mediaassist/raw/shared_transporter/Alanine/shared_transporter_Alanine_Arginine__0302721007e4.xlsx
    Options set to the site's "all" are left out of the name.
    """
    params = params or {}
    comp = params.get("component") or "-".join(params.get("selection") or []) or None
    if not comp and original_name:
        inferred = infer_type(original_name)
        comp = "-".join(inferred[1].get("selection", [])) if inferred else Path(original_name).stem
    comp = _slug(comp or "unknown")
    extras = [params["partner"]] if params.get("partner") else []
    extras += [v for k, v in params.items() if k not in _ID_KEYS | {"partner"} and v not in (None, "", "all")]
    name = "_".join([query_type, comp, *map(_slug, extras)])
    return f"mediaassist/raw/{query_type}/{comp}/{name}__{digest[:12]}{ext.lower()}"


def ingest_bytes(data: bytes, ext: str, query_type: str, query_params: dict | None = None,
                 origin: str = "file", original_name: str | None = None) -> dict:
    digest = storage.sha256(data)
    key = s3_key_for(query_type, query_params, digest, ext, original_name)

    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id, row_count FROM mediaassist_downloads WHERE query_type=%s AND sha256=%s",
            (query_type, digest),
        ).fetchone()
        if existing:
            return {"download_id": existing["id"], "rows": existing["row_count"], "new_dois": 0, "duplicate": True}

        df = _parse(data, ext)
        df.columns = [str(c).strip() for c in df.columns]
        storage.put(key, data, _EXT_TYPES.get(ext.lower(), "application/octet-stream"),
                    {"query-type": query_type, "sha256": digest})

        with conn.transaction():
            dl_id = conn.execute(
                """INSERT INTO mediaassist_downloads
                       (query_type, query_params, origin, original_name, s3_bucket, s3_key, sha256, row_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (query_type, json.dumps(query_params or {}), origin, original_name,
                 settings.s3_bucket, key, digest, len(df)),
            ).fetchone()["id"]

            # rows.dois = every DOI string seen in the row (raw, unverified). Papers are
            # registered only after citations.resolve() has checked them against titles.
            all_dois: list[str] = []
            records = []
            for i, row in enumerate(df.to_dict(orient="records")):
                row = {k: (None if pd.isna(v) or str(v).strip() in ("", "nan") else str(v).strip()) for k, v in row.items()}
                if not any(row.values()):
                    continue
                dois = find_all(" ".join(v for v in row.values() if v))
                all_dois.extend(dois)
                records.append((dl_id, query_type, i, json.dumps(row, ensure_ascii=False), dois))
            inserted = 0
            with conn.cursor() as cur:
                for rec in records:  # facts already stored from another export are skipped
                    cur.execute(
                        """INSERT INTO mediaassist_rows (download_id, query_type, row_num, data, dois) VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (query_type, md5(data::text)) DO NOTHING""",
                        rec,
                    )
                    inserted += cur.rowcount

    return {"download_id": dl_id, "rows": inserted, "rows_in_file": len(records), "already_known": len(records) - inserted,
            "dois_seen": len(set(all_dois)), "duplicate": False}


def ingest_file(path: str | Path, query_type: str, query_params: dict | None = None) -> dict:
    p = Path(path)
    return ingest_bytes(p.read_bytes(), p.suffix or ".csv", query_type, query_params, "file", p.name)


# MediaAssist export filename prefix -> query type. Longest prefix first
# ("shared_transporters_data" must win over "transporter_data").
FILENAME_TYPES = [
    ("shared_transporters_data", "shared_transporter"),
    ("uptake_rate_data", "uptake_rate"),
    ("concentration_data", "concentration"),
    ("solubility_data", "solubility"),
    ("stability_data", "stability"),
    ("transporter_data", "transporter"),
    ("codependency", "codependency"),
]


def infer_type(filename: str) -> tuple[str, dict] | None:
    """'uptake_rate_data_Alanine.xlsx' -> ('uptake_rate', {'selection': ['Alanine']})"""
    stem = Path(filename).stem
    for prefix, qtype in FILENAME_TYPES:
        if stem.lower().startswith(prefix):
            rest = stem[len(prefix):].strip("_")
            return qtype, ({"selection": rest.split("_")} if rest else {})
    return None


def ingest_dir(folder: str | Path) -> list[dict]:
    results = []
    for p in sorted(Path(folder).iterdir()):
        if p.suffix.lower() not in _EXT_TYPES:
            continue
        inferred = infer_type(p.name)
        if not inferred:
            results.append({"file": p.name, "error": "unknown query type; use `ingest --type`"})
            continue
        qtype, params = inferred
        results.append({"file": p.name, "query_type": qtype, **ingest_file(p, qtype, params)})
    return results


# ---------------------------------------------------------------------------
# Automated harvesting
# ---------------------------------------------------------------------------

def _expand(params: dict) -> list[dict]:
    """{'component': ['a','b'], 'temp': 25} -> [{'component':'a','temp':25}, {'component':'b','temp':25}]"""
    keys = list(params)
    values = [v if isinstance(v, list) else [v] for v in params.values()]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def harvest(config_path: str | Path, delay: float = 1.0, dry_run: bool = False) -> list[dict]:
    """Run every query in the config. See config/mediaassist_queries.example.json."""
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    results = []
    with httpx.Client(headers={"User-Agent": "lemkb/0.1"}, follow_redirects=True, timeout=120) as c:
        if cfg.get("warmup_url"):  # e.g. load the form page first to get session cookies
            c.get(cfg["warmup_url"])
        for q in cfg["queries"]:
            for params in _expand(q.get("params", {})):
                label = f"{q['query_type']} {params}"
                if dry_run:
                    log.info("DRY RUN %s %s %s", q.get("method", "GET"), q["url"], label)
                    continue
                method = q.get("method", "GET").upper()
                r = c.request(method, q["url"], **({"params": params} if method == "GET" else {"data": params}))
                if r.status_code != 200 or not r.content:
                    log.warning("HTTP %s for %s", r.status_code, label)
                    results.append({"query": label, "error": f"HTTP {r.status_code}"})
                else:
                    ext = q.get("format") or _guess_ext(r.headers.get("content-type", ""))
                    res = ingest_bytes(r.content, ext, q["query_type"], params, "harvest")
                    log.info("%s -> %s", label, res)
                    results.append({"query": label, **res})
                time.sleep(delay)
    return results


def _guess_ext(ctype: str) -> str:
    ctype = ctype.lower()
    if "spreadsheetml" in ctype:
        return ".xlsx"
    if "ms-excel" in ctype:
        return ".xls"
    if "html" in ctype:
        return ".html"
    if "tab-separated" in ctype:
        return ".tsv"
    return ".csv"
