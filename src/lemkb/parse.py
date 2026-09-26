"""Layer 3: stored papers -> ordered, citable passages.

    PDF        -> GROBID (sections, paragraphs, figure captions, page numbers)
                  fallback: pypdf page text (pages, no sections) if GROBID is down or fails
    JATS XML   -> parsed directly (PMC / publisher XML: sections, paragraphs, captions)
    no file    -> title + abstract, so papers we couldn't get are still searchable

Output goes to Postgres (passages) and to S3 as JSON (parsed/<doi>.json) for bulk export.
A paper is re-parsed automatically when its stored file changes (sha256).
"""
from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import asdict, dataclass

import httpx
from lxml import etree

from . import db, storage
from .config import settings
from .doi import s3_key

log = logging.getLogger("lemkb.parse")
logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_CHARS = 2500  # split longer paragraphs at sentence boundaries (keeps passages RAG-sized)
TEI = {"t": "http://www.tei-c.org/ns/1.0"}


@dataclass
class Passage:
    kind: str
    text: str
    section: str | None = None
    page: int | None = None


def _norm(t: str | None) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _split(p: Passage) -> list[Passage]:
    if len(p.text) <= MAX_CHARS:
        return [p]
    out, buf = [], ""
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", p.text):
        if buf and len(buf) + len(sent) > MAX_CHARS:
            out.append(Passage(p.kind, buf.strip(), p.section, p.page))
            buf = ""
        buf += sent + " "
    if buf.strip():
        out.append(Passage(p.kind, buf.strip(), p.section, p.page))
    return out


# ---------------------------------------------------------------------------
# PDF via GROBID
# ---------------------------------------------------------------------------

def grobid_version() -> str | None:
    try:
        return httpx.get(f"{settings.grobid_url}/api/version", timeout=5).text.strip() or None
    except httpx.HTTPError:
        return None


def _first_page(el) -> int | None:
    coords = el.get("coords")
    if not coords:
        return None
    try:
        return int(coords.split(";")[0].split(",")[0])
    except ValueError:
        return None


def parse_pdf_grobid(data: bytes) -> list[Passage]:
    r = httpx.post(
        f"{settings.grobid_url}/api/processFulltextDocument",
        files={"input": ("paper.pdf", data, "application/pdf")},
        data={"teiCoordinates": ["p", "head", "figure"], "consolidateHeader": "0"},
        timeout=300,
    )
    r.raise_for_status()
    root = etree.fromstring(r.content)
    out: list[Passage] = []

    title = _norm(" ".join(root.xpath("//t:teiHeader//t:titleStmt/t:title//text()", namespaces=TEI)))
    if title:
        out.append(Passage("title", title, None, 1))
    for p in root.xpath("//t:profileDesc/t:abstract//t:p", namespaces=TEI):
        if txt := _norm("".join(p.itertext())):
            out.append(Passage("abstract", txt, "Abstract", _first_page(p) or 1))

    for div in root.xpath("//t:text/t:body/t:div", namespaces=TEI):
        head = div.find("t:head", TEI)
        section = _norm("".join(head.itertext())) if head is not None else None
        if head is not None and head.get("n"):
            section = f"{head.get('n')} {section}"
        for p in div.xpath("./t:p", namespaces=TEI):
            if txt := _norm("".join(p.itertext())):
                out.append(Passage("body", txt, section, _first_page(p)))

    for fig in root.xpath("//t:text//t:figure", namespaces=TEI):
        label = (_norm(" ".join(fig.xpath("./t:head//text()", namespaces=TEI)))
                 or _norm(" ".join(fig.xpath("./t:label//text()", namespaces=TEI))))
        desc = _norm(" ".join(fig.xpath("./t:figDesc//text()", namespaces=TEI)))
        kind = "table" if fig.get("type") == "table" else "caption"
        rows = fig.xpath(".//t:table//t:row", namespaces=TEI)
        table_txt = " | ".join(_norm(" ".join(r.itertext())) for r in rows) if rows else ""
        desc = re.sub(r"^((?:Fig(?:ure)?|Table)\.?\s?\d+[.:]?)(?=[A-Z])", r"\1 ", desc)  # "Fig.1Effect" -> "Fig.1 Effect"
        squash = lambda t: re.sub(r"\W", "", t).lower()
        if label and squash(desc).startswith(squash(label)):
            label_prefix = ""  # caption text already starts with "Fig. 3" / "Table 1"
        else:
            label_prefix = f"{label}. " if label else ""
        txt = _norm(f"{label_prefix}{desc} {table_txt}") if (desc or table_txt) else ""
        if txt:
            out.append(Passage(kind, txt, label or None, _first_page(fig)))
    return out


def parse_pdf_pypdf(data: bytes) -> list[Passage]:
    from pypdf import PdfReader
    out = []
    for i, page in enumerate(PdfReader(io.BytesIO(data)).pages, 1):
        for para in re.split(r"\n\s*\n", page.extract_text() or ""):
            if len(txt := _norm(para)) > 40:
                out.append(Passage("body", txt, None, i))
    return out


# ---------------------------------------------------------------------------
# JATS XML (PubMed Central and most publisher XML)
# ---------------------------------------------------------------------------

def parse_jats(data: bytes) -> list[Passage]:
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(data, parser)
    for el in root.iter():  # drop namespaces so xpath stays simple
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    out: list[Passage] = []
    if t := _norm(" ".join(root.xpath("//article-meta//article-title//text()"))):
        out.append(Passage("title", t))
    for p in root.xpath("//article-meta/abstract//p"):
        if txt := _norm("".join(p.itertext())):
            out.append(Passage("abstract", txt, "Abstract"))

    def walk(sec, path):
        title = _norm(" ".join(sec.xpath("./title//text()")))
        name = " > ".join(filter(None, path + [title])) or None
        for child in sec:
            if child.tag == "p" and (txt := _norm("".join(child.itertext()))):
                out.append(Passage("body", txt, name))
            elif child.tag == "sec":
                walk(child, path + ([title] if title else []))

    body = root.find(".//body")
    if body is not None:
        loose = [c for c in body if c.tag == "p"]
        for p in loose:
            if txt := _norm("".join(p.itertext())):
                out.append(Passage("body", txt))
        for sec in body.findall("sec"):
            walk(sec, [])
    for fig in root.xpath("//fig | //table-wrap"):
        label = _norm(" ".join(fig.xpath("./label//text()")))
        cap = _norm(" ".join(fig.xpath("./caption//text()")))
        if cap:
            out.append(Passage("table" if fig.tag == "table-wrap" else "caption", _norm(f"{label}. {cap}"), label or None))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _parse_paper(paper: dict) -> tuple[list[Passage], str, str | None, str]:
    """-> (passages, parser, parser_version, source)"""
    if paper["status"] != "downloaded":
        ps = [Passage("title", paper["title"])] if paper["title"] else []
        if paper["abstract"]:
            ps.append(Passage("abstract", paper["abstract"], "Abstract"))
        return ps, "metadata", None, "abstract"

    data = storage.client().get_object(Bucket=settings.s3_bucket, Key=paper["s3_key"])["Body"].read()
    if paper["content_type"] == "application/xml":
        return parse_jats(data), "jats", None, "full_text"
    version = grobid_version()
    if version:
        try:
            ps = parse_pdf_grobid(data)
            if sum(p.kind == "body" for p in ps) >= 3:
                return ps, "grobid", version, "full_text"
            log.warning("%s: GROBID found little body text, falling back to pypdf", paper["doi"])
        except Exception as e:
            log.warning("%s: GROBID failed (%s), falling back to pypdf", paper["doi"], e)
    return parse_pdf_pypdf(data), "pypdf", None, "full_text"


def parse(doi: str | None = None, force: bool = False) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT p.doi, p.status, p.title, p.abstract, p.s3_bucket, p.s3_key, p.sha256, p.content_type
               FROM papers p LEFT JOIN paper_texts t USING (doi)
               WHERE (p.status = 'downloaded' OR p.abstract IS NOT NULL OR p.title IS NOT NULL)
                 AND (%(doi)s::text IS NULL OR p.doi = %(doi)s)
                 AND (%(force)s OR t.doi IS NULL
                      OR t.source_sha256 IS DISTINCT FROM p.sha256
                      OR (t.source = 'abstract' AND p.status = 'downloaded'))
               ORDER BY p.doi""",
            {"doi": doi, "force": force},
        ).fetchall()
        counts: dict[str, int] = {}
        for paper in rows:
            d = paper["doi"]
            try:
                ps, parser, version, source = _parse_paper(paper)
                ps = [q for p in ps for q in _split(p) if q.text]
                key = s3_key(d, "json").replace("papers/", "parsed/", 1)
                export = {"doi": d, "source": source, "parser": parser, "parser_version": version,
                          "source_sha256": paper["sha256"],
                          "passages": [dict(asdict(p), passage_id=f"{d}#p{i}", seq=i) for i, p in enumerate(ps)]}
                storage.put(key, json.dumps(export, ensure_ascii=False).encode(), "application/json", {"doi": d})
                with conn.transaction():
                    conn.execute("DELETE FROM passages WHERE doi=%s", (d,))
                    with conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO passages (doi, seq, kind, section, page, text) VALUES (%s,%s,%s,%s,%s,%s)",
                            [(d, i, p.kind, p.section, p.page, p.text) for i, p in enumerate(ps)],
                        )
                    _record(conn, d, source, paper["sha256"], parser, version, key, len(ps), "parsed", None)
                label = f"{parser}"
                log.info("%-8s %4d passages  %s", parser, len(ps), d)
            except Exception as e:
                log.exception("%s: parse failed", d)
                _record(conn, d, "full_text", paper["sha256"], "error", None, None, 0, "failed", str(e)[:1000])
                label = "failed"
            counts[label] = counts.get(label, 0) + 1
    return counts


def _record(conn, doi, source, sha, parser, version, key, n, status, error):
    conn.execute(
        """INSERT INTO paper_texts (doi, source, source_sha256, parser, parser_version, s3_key, n_passages, status, error, parsed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (doi) DO UPDATE SET source=EXCLUDED.source, source_sha256=EXCLUDED.source_sha256,
             parser=EXCLUDED.parser, parser_version=EXCLUDED.parser_version, s3_key=EXCLUDED.s3_key,
             n_passages=EXCLUDED.n_passages, status=EXCLUDED.status, error=EXCLUDED.error, parsed_at=now()""",
        (doi, source, sha, parser, version, key, n, status, error),
    )
