"""Spot-check validation: random facts are re-verified end to end against independent sources.

For each sampled fact:
  1 live_mediaassist  the fact still exists on MediaAssist, checked via its live JSON endpoints
                      (the harvest used the Excel downloads, so this is an independent path)
  2 citation_doi      Crossref's registered title for our DOI matches the citation text (fresh lookup)
  3 s3_integrity      the paper's object exists in S3 and its sha256 matches the catalog
  4 file_is_paper     the stored PDF/XML's own text contains the paper's title
  5 query_path        the API evidence lookup returns the same DOI, and the file it serves is the
                      S3 object byte for byte
  6 value_in_paper    (informational) the fact's value / wording appears in the paper's text

Results: PASS / FAIL / SKIP (not applicable, e.g. no full text) / UNVERIFIABLE (e.g. scanned PDF).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import random
import re
from pathlib import Path

import httpx

from . import db, service, storage
from .citations import MATCH_THRESHOLD, _Registry, title_match
from .config import settings
from .harvest import Harvester, component_names
from .inbox import _first_pages_text

log = logging.getLogger("lemkb.validate")
ROOT = Path(__file__).resolve().parents[2]

LIVE = {  # query type -> (JSON endpoint, payload builder)
    "concentration": ("/get_concentration_data", lambda ids, d: {"component_id_conc": ids[0], "medium_conc": "all"}),
    "solubility": ("/get_solubility_data", lambda ids, d: {"component_id": ids[0], "temperature": d.get("Temperature (°C)")}),
    "stability": ("/get_stability_data", lambda ids, d: {"component_id": ids[0], "temperature": "all", "ph": "all"}),
    "uptake_rate": ("/get_uptake_rate_data", lambda ids, d: {"component_id": ids[0], "medium_uptake_measured": "all"}),
    "transporter": ("/get_transporter_data", lambda ids, d: {"component_id": ids[0], "mechanism": "all"}),
    "shared_transporter": ("/get_shared_transporters", lambda ids, d: {"component_1_id": ids[0], "component_2_id": ids[1]}),
    "codependency": ("/get_interaction_data", lambda ids, d: {"component_id": ids[0], "interaction_type": "all"}),
}
COMP_KEYS = {"shared_transporter": ("Selected Component", "Other Component"),
             "codependency": ("Selected component", "Component 1")}
IGNORE = re.compile(r"link|^year$", re.I)  # Excel-only helper columns


# ---------------------------------------------------------------------------
# value normalisation shared by live comparison and the in-paper check
# ---------------------------------------------------------------------------

def _num(s) -> float | None:
    """'4.60×10<sup>-1</sup>' | '-17.6315 x10^-5' | '0.4602' | '16400 mg/L' -> float"""
    s = str(s).replace("−", "-").replace("<sup>", "^").replace("</sup>", "")
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)(?:\s*[×x]\s*10\^?\s*(-?\d+))?", s)
    if not m:
        return None
    v = float(m.group(1))
    return v * 10 ** int(m.group(2)) if m.group(2) else v


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _flatten(record) -> list[str]:
    out = []
    for v in record.values():
        if isinstance(v, list):
            out += [x.get("text", "") if isinstance(x, dict) else str(x) for x in v]
        elif v is not None:
            out.append(str(v))
    return out


def _value_found(value: str, live_values: list[str]) -> bool:
    if _exact_found(value, live_values):
        return True
    # heterodimers: the Excel export writes "SLC7A8+SLC3A2", the JSON lists the subunits
    # separately. Only a fallback, so aliases like "y+LAT2" are still matched whole first.
    for sep in ("|", "+"):  # "A | B" = several citations; the JSON lists them separately
        parts = [p.strip() for p in str(value).strip().split(sep)]
        if len(parts) > 1 and all(p and _exact_found(p, live_values) for p in parts):
            return True
    return False


def _exact_found(value: str, live_values: list[str]) -> bool:
    n = _num(value)
    if n is not None and re.fullmatch(r"\s*-?[\d.]+(\s*[×x].*)?\s*", str(value)):
        nums = [x for x in map(_num, live_values) if x is not None]
        return any(abs(x - n) <= max(1e-9, 0.01 * abs(n)) for x in nums)
    nv = _norm(value)
    return any(nv == _norm(x) or (len(nv) > 12 and nv in _norm(x)) for x in live_values)


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

class Validator:
    def __init__(self):
        self.h = Harvester(delay=1.5)
        self.reg = _Registry(httpx.Client(timeout=30, follow_redirects=True))
        self.names = {v.lower(): k for k, v in component_names().items()}
        self.live_cache: dict[str, list] = {}
        from fastapi.testclient import TestClient
        from .api import app
        self.api = TestClient(app)

    def live_mediaassist(self, f: dict) -> tuple[str, str]:
        d, qt = f["data"], f["query_type"]
        keys = COMP_KEYS.get(qt, ("Component Name",))
        comps = [d.get(k) for k in keys if d.get(k)]
        if qt == "shared_transporter":
            comps = [d.get("Selected Component"), d.get("Other Component")]
        elif qt == "codependency":
            comps = [d.get("Selected component") or d.get("Component 1")]
        ids = [self.names.get((c or "").lower()) for c in comps]
        if None in ids:
            return "FAIL", f"component not on MediaAssist: {comps}"
        url, build = LIVE[qt]
        payload = build(ids, d)
        ck = json.dumps([url, payload], sort_keys=True)
        if ck not in self.live_cache:
            r = self.h.request("POST", url, data=payload)
            self.live_cache[ck] = r.json() if r is not None and r.status_code == 200 else None
        live = self.live_cache[ck]
        if live is None:
            return "FAIL", "live endpoint returned no data"
        if qt == "codependency" and d.get("Component 2") and not d.get("Other component"):
            # pair export (Component 1/2 headers): the per-component listing may name them the other way round
            pass
        ours = {k: v for k, v in d.items() if v and not IGNORE.search(k)}
        best, best_score, best_missing = None, -1.0, []
        for rec in live:
            vals = _flatten(rec)
            missing = [k for k, v in ours.items() if not _value_found(v, vals)]
            score = 1 - len(missing) / max(1, len(ours))
            if score > best_score:
                best, best_score, best_missing = rec, score, missing
        # the pmol/cell/day column exists only in the Excel export (MediaAssist's own unit conversion)
        tolerated = [m for m in best_missing if "pmol/cell/day" not in m]
        if not tolerated:
            return "PASS", f"matched a live record ({len(live)} live rows for this query)"
        return "FAIL", f"closest live record differs in: {tolerated}"

    def citation_doi(self, f: dict) -> tuple[str, str]:
        c = f["citation"]
        if c["source_kind"] == "non_doi":
            return "SKIP", "source has no DOI (e.g. handbook / web page)"
        if not c["doi"]:
            return "FAIL", "no DOI resolved"
        title = self.reg.title(c["doi"])
        score = title_match(title, c["text"])
        verdict = "PASS" if score >= MATCH_THRESHOLD else "FAIL"
        return verdict, f"Crossref title {title!r} vs citation: score {score}"

    def s3_and_paper(self, f: dict) -> dict[str, tuple[str, str]]:
        c = f["citation"]
        p = service.get_paper(c["doi"], with_file_link=False) if c.get("doi") else None
        if not p or p["status"] != "downloaded":
            skip = ("SKIP", f"no full text in the vault ({p['status'] if p else 'no paper'})")
            return {"s3_integrity": skip, "file_is_paper": skip, "query_path": skip, "value_in_paper": skip}
        out = {}
        try:
            body = storage.client().get_object(Bucket=settings.s3_bucket, Key=p["s3_key"])["Body"].read()
        except Exception as e:
            fail = ("FAIL", f"S3 object {p['s3_key']} not readable: {e}")
            return {"s3_integrity": fail, "file_is_paper": fail, "query_path": fail, "value_in_paper": ("SKIP", "")}
        digest = hashlib.sha256(body).hexdigest()
        out["s3_integrity"] = (("PASS", f"{p['s3_key']} ({len(body)} bytes), sha256 matches")
                               if digest == p["sha256"] else ("FAIL", f"sha256 mismatch for {p['s3_key']}"))
        text = _first_pages_text(body)
        score = title_match(p["title"], text)
        if len(text.strip()) < 200:
            out["file_is_paper"] = ("UNVERIFIABLE", "file has no text layer (scanned image)")
        else:
            out["file_is_paper"] = (("PASS" if score >= MATCH_THRESHOLD else "FAIL"),
                                    f"title found in file text: score {score}")
        ev = self.api.get(f"/api/facts/{f['fact_id']}/evidence").json()
        served = self.api.get(f"/api/papers/{c['doi'].replace('/', '%2F')}/file")
        same_doi = ev["citation"]["doi"] == c["doi"]
        same_file = served.status_code == 200 and hashlib.sha256(served.content).hexdigest() == digest
        out["query_path"] = (("PASS", "evidence lookup -> same DOI; served file == S3 object")
                             if same_doi and same_file else
                             ("FAIL", f"doi match {same_doi}, file match {same_file} (HTTP {served.status_code})"))
        with db.connect() as conn:
            full = " ".join(r["text"] for r in conn.execute(
                "SELECT text FROM passages WHERE doi=%s ORDER BY seq", (c["doi"],)).fetchall())
        out["value_in_paper"] = self._value_in_paper(f, full)
        return out

    @staticmethod
    def _value_in_paper(f: dict, full: str) -> tuple[str, str]:
        d, qt = f["data"], f["query_type"]
        probe = {"concentration": d.get("Concentration (mM)"), "stability": d.get("Stability"),
                 "uptake_rate": d.get("Specific Uptake Rate"), "solubility": d.get("Aqueous Solubility"),
                 "transporter": d.get("Transporter"), "shared_transporter": d.get("Shared Transporter"),
                 "codependency": d.get("Other component") or d.get("Component 2")}.get(qt)
        if not probe:
            return "SKIP", "no single value to look for"
        n = _num(probe)
        if n is not None and qt in ("concentration", "stability", "uptake_rate", "solubility"):
            nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", full)]
            hit = any(abs(abs(x) - abs(n)) <= 0.01 * abs(n) for x in nums if n)
            return ("PASS" if hit else "INFO"), (f"value {n:g} appears in the paper text" if hit else
                    f"value {n:g} not found verbatim (often in a figure/table, or unit-converted)")
        hit = _norm(probe) in _norm(full)
        return ("PASS" if hit else "INFO"), f"'{probe}' {'appears' if hit else 'not found'} in the paper text"


def run(n_per_type: int = 4, n_full_text: int = 10, seed: int = 42) -> dict:
    rng = random.Random(seed)
    with db.connect() as conn:
        by_type = {}
        for r in conn.execute("SELECT row_id, query_type, paper_status FROM fact_evidence").fetchall():
            by_type.setdefault(r["query_type"], []).append(r)
    picked = set()
    for qt, rows in sorted(by_type.items()):
        picked |= {r["row_id"] for r in rng.sample(rows, min(n_per_type, len(rows)))}
    with_text = [r["row_id"] for rows in by_type.values() for r in rows if r["paper_status"] == "downloaded" and r["row_id"] not in picked]
    picked |= set(rng.sample(with_text, min(n_full_text, len(with_text))))

    v = Validator()
    results = []
    for fid in sorted(picked):
        f = service.evidence_for_fact(fid, limit=1)
        row = {"fact_id": fid, "query_type": f["query_type"], "doi": f["citation"]["doi"] or "",
               "component": next((f["data"].get(k) for k in ("Component Name", "Selected component", "Selected Component", "Component 1") if f["data"].get(k)), "")}
        checks = {"live_mediaassist": v.live_mediaassist(f), "citation_doi": v.citation_doi(f), **v.s3_and_paper(f)}
        for k, (verdict, detail) in checks.items():
            row[k], row[k + "_detail"] = verdict, detail
        results.append(row)
        log.info("fact %-5s %-18s %s", fid, f["query_type"], " ".join(f"{k}={checks[k][0]}" for k in checks))

    out = ROOT / "data" / "validation_report.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    names = ["live_mediaassist", "citation_doi", "s3_integrity", "file_is_paper", "query_path", "value_in_paper"]
    summary = {k: {s: sum(r[k] == s for r in results) for s in ("PASS", "FAIL", "SKIP", "UNVERIFIABLE", "INFO") if any(r[k] == s for r in results)} for k in names}
    return {"facts_checked": len(results), "summary": summary, "report": str(out),
            "failures": [{"fact_id": r["fact_id"], "check": k, "detail": r[k + "_detail"]}
                         for r in results for k in names[:5] if r[k] == "FAIL"]}
