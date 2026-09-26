"""Automated MediaAssist harvest: every component x every query type, via the site's own
Excel download endpoints (the same files its Download buttons produce).

For each query type we list the components, list that component's options (media,
temperatures, mechanisms, partners ...), prefer the site's "all" option when offered,
and download the Excel export. Each export goes through the normal ingest path
(raw file -> S3, rows -> Postgres), so harvested data is identical in shape to
hand-downloaded files.

Polite by design: one request at a time, DELAY seconds apart, retries with backoff,
and it stops after repeated failures. Resumable: requests already done are skipped.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from . import db, mediaassist

log = logging.getLogger("lemkb.harvest")

BASE = "https://mediaassist.ncl.res.in"
DELAY = 1.5
MAX_CONSECUTIVE_FAILURES = 8
XLSX_MAGIC = b"PK\x03\x04"


@dataclass
class Plan:
    query_type: str
    components_url: str
    download_url: str
    # component -> list of form payloads for the download endpoint
    payloads: Callable[["Harvester", dict], list[dict]]


def _with_all(values: list, key: str) -> list:
    """Use the site's 'all' option if it offers one; otherwise every value."""
    values = [v for v in values if v not in (None, "")]
    return ["all"] if "all" in values else values


class Harvester:
    def __init__(self, delay: float = DELAY):
        self.delay = delay
        self.c = httpx.Client(base_url=BASE, timeout=90, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (lemkb media-kb harvester; low-rate)"})
        self.last = 0.0
        self.failures = 0

    def _wait(self):
        gap = self.last + self.delay - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        self.last = time.monotonic()

    def request(self, method: str, url: str, **kw) -> httpx.Response | None:
        for attempt in range(4):
            self._wait()
            try:
                r = self.c.request(method, url, **kw)
                if r.status_code < 500 and r.status_code != 429:
                    self.failures = 0 if r.status_code == 200 else self.failures
                    return r
                log.warning("%s %s -> HTTP %s (retrying)", method, url, r.status_code)
            except httpx.HTTPError as e:
                log.warning("%s %s -> %s (retrying)", method, url, e)
            time.sleep(3 * 2 ** attempt)
        self.failures += 1
        if self.failures >= MAX_CONSECUTIVE_FAILURES:
            raise RuntimeError("MediaAssist keeps failing; stopping to avoid hammering the site. Try again later.")
        return None

    def get_json(self, url: str):
        r = self.request("GET", url)
        return r.json() if r is not None and r.status_code == 200 else None


# ---------------------------------------------------------------------------
# One plan per MediaAssist query type
# ---------------------------------------------------------------------------

def _concentration(h, comp):
    media = (h.get_json(f"/get_concentration_media/{comp['unique_id']}") or {}).get("media", [])
    return [{"component_id_conc": comp["unique_id"], "medium_conc": m} for m in _with_all(media, "medium")]


def _solubility(h, comp):
    temps = (h.get_json(f"/get_solubility_temperatures_ph/{comp['unique_id']}") or {}).get("temperatures", [])
    return [{"component_id": comp["unique_id"], "temperature": t} for t in _with_all(temps, "temperature")]


def _stability(h, comp):
    opts = h.get_json(f"/get_stability_temperatures_ph/{comp['unique_id']}") or {}
    return [{"component_id": comp["unique_id"], "temperature": t, "ph": p}
            for t in _with_all(opts.get("temperatures", []), "t") for p in _with_all(opts.get("ph_values", []), "p")]


def _uptake(h, comp):
    media = h.get_json(f"/get_media_for_component/{comp['unique_id']}") or []
    return [{"component_id": comp["unique_id"], "medium_uptake_measured": m} for m in _with_all(media, "medium")]


def _transporter(h, comp):
    mechs = h.get_json(f"/get_transport_mechanisms_for_component/{comp['unique_id']}") or []
    return [{"component_id": comp["unique_id"], "mechanism": m} for m in _with_all(mechs, "mechanism")]


def _shared(h, comp):
    partners = h.get_json(f"/get_components_with_shared_transporters_dropdown2/{comp['unique_id']}") or []
    # each unordered pair once: only partners with a higher id than this component
    # "_partner" is a label for our records only; keys starting with "_" are not sent to the site
    return [{"component_1_id": comp["unique_id"], "component_2_id": p["unique_id"], "_partner": p["name"]}
            for p in partners if p["unique_id"] > comp["unique_id"]]


def _codependency(h, comp):
    types = h.get_json(f"/get_interaction_types_for_component/{comp['unique_id']}") or []
    return [{"component_id": comp["unique_id"], "interaction_type": t} for t in _with_all(types, "type")]


PLANS = [
    Plan("concentration", "/get_components_with_concentration", "/download_concentration_excel", _concentration),
    Plan("solubility", "/get_components_with_solubility", "/download_solubility_excel", _solubility),
    Plan("stability", "/get_components_with_stability", "/download_stability_excel", _stability),
    Plan("uptake_rate", "/get_components_with_uptake_rate", "/download_uptake_rate_excel", _uptake),
    Plan("transporter", "/get_components_with_transporters", "/download_transporter_excel", _transporter),
    Plan("shared_transporter", "/get_components_with_shared_transporters", "/download_shared_transporters_excel", _shared),
    Plan("codependency", "/get_components_with_interactions", "/download_interaction_excel", _codependency),
]


def _already_done(conn, query_type: str, params: dict) -> bool:
    # containment, so records made before a label field was added still count as done
    core = {k: v for k, v in params.items() if k != "partner"}
    return conn.execute(
        "SELECT 1 FROM mediaassist_downloads WHERE query_type=%s AND origin='harvest' AND query_params @> %s::jsonb",
        (query_type, json.dumps(core)),
    ).fetchone() is not None


def run(query_types: list[str] | None = None, components: list[str] | None = None,
        delay: float = DELAY, dry_run: bool = False) -> dict:
    """Harvest everything (or a subset). Safe to re-run: finished requests are skipped."""
    h = Harvester(delay)
    wanted = {c.lower() for c in components} if components else None
    summary: dict[str, dict] = {}
    try:
        for plan in PLANS:
            if query_types and plan.query_type not in query_types:
                continue
            stats = summary.setdefault(plan.query_type, {"components": 0, "downloads": 0, "skipped": 0,
                                                         "empty": 0, "failed": 0, "rows_added": 0})
            comps = h.get_json(plan.components_url) or []
            comps = [c for c in comps if not wanted or c["name"].lower() in wanted]
            stats["components"] = len(comps)
            log.info("%s: %d components", plan.query_type, len(comps))
            for i, comp in enumerate(comps, 1):
                for payload in plan.payloads(h, comp):
                    labels = {k.lstrip("_"): payload.pop(k) for k in list(payload) if k.startswith("_")}
                    params = {"component": comp["name"], **labels, **payload}
                    with db.connect() as conn:
                        if _already_done(conn, plan.query_type, params):
                            stats["skipped"] += 1
                            continue
                    if dry_run:
                        log.info("DRY %s %s", plan.query_type, params)
                        continue
                    r = h.request("POST", plan.download_url, data=payload)
                    if r is None or r.status_code != 200:
                        stats["failed"] += 1
                        log.warning("[%s %d/%d] %s: download failed (%s)", plan.query_type, i, len(comps),
                                    comp["name"], r.status_code if r is not None else "no response")
                        continue
                    if not r.content.startswith(XLSX_MAGIC):
                        stats["empty"] += 1
                        log.info("[%s %d/%d] %s: no data (%s)", plan.query_type, i, len(comps), comp["name"],
                                 r.headers.get("content-type"))
                        continue
                    name = f"{plan.query_type}_{comp['name']}.xlsx"
                    res = mediaassist.ingest_bytes(r.content, ".xlsx", plan.query_type, params, "harvest", name)
                    stats["downloads"] += 1
                    stats["rows_added"] += res.get("rows", 0)
                    log.info("[%s %d/%d] %-22s +%d rows", plan.query_type, i, len(comps), comp["name"], res.get("rows", 0))
    finally:
        h.c.close()
    return summary


def component_names() -> dict[int, str]:
    """MediaAssist component id -> name (ids are global across query types)."""
    h = Harvester()
    try:
        names = {}
        for plan in PLANS:
            for c in h.get_json(plan.components_url) or []:
                names[c["unique_id"]] = c["name"]
        return names
    finally:
        h.c.close()


def tidy_raw_keys() -> dict[str, int]:
    """One-time: move raw exports stored under hash-only keys to browsable per-component keys."""
    from . import storage
    from .config import settings
    from .mediaassist import s3_key_for

    s3 = storage.client()
    with db.connect() as conn:
        rows = conn.execute("SELECT id, query_type, query_params, original_name, sha256, s3_key FROM mediaassist_downloads").fetchall()
        names = None
        moved = 0
        for r in rows:
            params = dict(r["query_params"] or {})
            if r["query_type"] == "shared_transporter" and "component_2_id" in params and "partner" not in params:
                names = names or component_names()
                params["partner"] = names.get(params["component_2_id"], str(params["component_2_id"]))
            ext = "." + r["s3_key"].rsplit(".", 1)[-1]
            new_key = s3_key_for(r["query_type"], params, r["sha256"], ext, r["original_name"])
            if new_key == r["s3_key"]:
                continue
            s3.copy_object(Bucket=settings.s3_bucket, Key=new_key, CopySource={"Bucket": settings.s3_bucket, "Key": r["s3_key"]})
            if storage.sha256(s3.get_object(Bucket=settings.s3_bucket, Key=new_key)["Body"].read()) != r["sha256"]:
                raise RuntimeError(f"copy of {r['s3_key']} does not match its hash; stopping")
            conn.execute("UPDATE mediaassist_downloads SET s3_key=%s, query_params=%s WHERE id=%s",
                         (new_key, json.dumps(params), r["id"]))
            s3.delete_object(Bucket=settings.s3_bucket, Key=r["s3_key"])
            moved += 1
    return {"files": len(rows), "moved": moved}
