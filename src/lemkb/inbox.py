"""Manual inbox: PDFs obtained by hand (library access, authors, purchase) -> matched -> S3.

Drop files into a folder, any filename, and run `lemkb inbox FOLDER`. For each file:

  1. find candidate DOIs: in the filename (10.1002_bit.26794.pdf, URL-encoded, ...),
     then in the text of the first pages
  2. no DOI -> compare the first pages against the titles of papers we're still missing
  3. verify: the paper's registered title must appear in the file's first pages
  4. store with full provenance (source = 'inbox'); move the file to FOLDER/processed/

A file that can't be matched with confidence is left in place and reported, never guessed.
"""
from __future__ import annotations

import io
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

from . import db, fetcher
from .citations import title_match
from .doi import find_all

log = logging.getLogger("lemkb.inbox")
logging.getLogger("pypdf").setLevel(logging.ERROR)  # font-encoding chatter on most journal PDFs

VERIFY_THRESHOLD = 0.75   # title of the DOI we found must appear in the file
SEARCH_THRESHOLD = 0.9    # title-only match (no DOI in file or name) must be near exact


def _first_pages_text(data: bytes, pages: int = 4) -> str:  # 4: publisher cover pages push the title back
    if not data.startswith(b"%PDF") and b"%PDF" not in data[:1024]:
        return data[:20000].decode("utf-8", errors="replace")  # XML
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return " ".join((reader.pages[i].extract_text() or "") for i in range(min(pages, len(reader.pages))))
    except Exception as e:  # damaged or encrypted PDF
        log.warning("could not read PDF text: %s", e)
        return ""


def _filename_dois(name: str) -> list[str]:
    stem = unquote(Path(name).stem)
    # 10.1002_bit.26794 -> 10.1002/bit.26794 (a "/" can't appear in a filename)
    variants = [stem, re.sub(r"^(10\.\d{4,9})[_\-\s]", r"\1/", stem)]
    return list(dict.fromkeys(d for v in variants for d in find_all(v)))


def identify(conn, path: Path, data: bytes) -> dict:
    text = _first_pages_text(data)
    papers = {r["doi"]: r for r in conn.execute("SELECT doi, title, status FROM papers").fetchall()}

    tried = []
    for how, dois in (("filename", _filename_dois(path.name)), ("pdf_text", find_all(text))):
        for d in dois:
            if d not in papers:
                continue
            score = title_match(papers[d]["title"], text)
            if score >= VERIFY_THRESHOLD:
                return {"doi": d, "how": how, "score": score, "verified": True}
            if how == "filename" and len(text.strip()) < 200:
                # Scanned PDF without a text layer: trust an explicit filename, but say so.
                return {"doi": d, "how": how, "score": None, "verified": False,
                        "note": "no text layer in file; matched on filename only"}
            tried.append(f"{d} via {how} (title score {score})")

    titled = [p for p in papers.values() if p["title"]]
    best = max(((title_match(p["title"], text), p["doi"]) for p in titled), default=(0.0, None))
    if best[0] >= SEARCH_THRESHOLD:
        return {"doi": best[1], "how": "title", "score": best[0], "verified": True}

    reason = "; ".join(tried) or f"no DOI in filename or text; best title match {best[0]}"
    return {"doi": None, "reason": reason}


def process(folder: str | Path, replace: bool = False) -> list[dict]:
    folder = Path(folder)
    done_dir = folder / "processed"
    results = []
    with db.connect() as conn:
        for path in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".pdf", ".xml")):
            data = path.read_bytes()
            m = identify(conn, path, data)
            if not m.get("doi"):
                results.append({"file": path.name, "result": "unmatched", "reason": m["reason"]})
                continue
            status = conn.execute("SELECT status FROM papers WHERE doi=%s", (m["doi"],)).fetchone()["status"]
            if status == "downloaded" and not replace:
                results.append({"file": path.name, "result": "already_have", "doi": m["doi"]})
                continue
            note = f"inbox match via {m['how']}" + (f", title score {m['score']}" if m.get("score") else "")
            if m.get("note"):
                note += f"; {m['note']}"
            stored = fetcher.attach_file(m["doi"], str(path), source="inbox", note=note)
            done_dir.mkdir(exist_ok=True)
            shutil.move(str(path), done_dir / path.name)
            results.append({"file": path.name, "result": "stored", "doi": m["doi"], "matched_via": m["how"],
                            "verified": m["verified"], "s3_key": stored["s3_key"]})
    return results
