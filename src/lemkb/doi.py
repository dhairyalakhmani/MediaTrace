"""DOI normalization and extraction."""
import re
from urllib.parse import unquote

# Crossref's recommended pattern, loosened slightly for DOIs embedded in text.
# "<" and ">" are allowed: old Wiley SICI DOIs contain them, e.g.
# 10.1002/(SICI)1097-0290(19970505)54:3<272::AID-BIT8>3.0.CO;2-C
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"']+", re.IGNORECASE)
_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:")
_TRAILING = ".,;:]}"


def normalize(raw: str) -> str | None:
    """Return a canonical lowercase DOI, or None if the string holds no DOI."""
    if not raw:
        return None
    s = unquote(str(raw).strip())
    for p in _PREFIXES:
        if s.lower().startswith(p):
            s = s[len(p):]
            break
    m = DOI_RE.search(s)
    if not m:
        return None
    doi = m.group(0).split("</", 1)[0].rstrip(_TRAILING)  # stop at a closing HTML tag
    # Unbalanced trailing ")" / ">" is almost always punctuation, not part of the DOI.
    while (doi.endswith(")") and doi.count("(") < doi.count(")")) or (doi.endswith(">") and doi.count("<") < doi.count(">")):
        doi = doi[:-1].rstrip(_TRAILING)
    return doi.lower()


def find_all(text: str) -> list[str]:
    """All distinct DOIs in a block of text, in order of appearance."""
    seen: dict[str, None] = {}
    for m in DOI_RE.finditer(str(text or "")):
        d = normalize(m.group(0))
        if d:
            seen.setdefault(d)
    return list(seen)


def flat_name(doi: str) -> str:
    """DOI -> flat, readable filename: 10.1016/j.jtemb.2016.12.016 -> 10.1016_j.jtemb.2016.12.016

    "/" and any character unsafe in file names or S3 consoles become "_". The DOI itself
    is always stored next to the key in Postgres, so the mapping never has to be reversed.
    """
    return re.sub(r"[^A-Za-z0-9._()-]", "_", doi)


def s3_key(doi: str, ext: str = "pdf") -> str:
    """Flat object key for a paper's file: papers/<flat_name>.<ext> (no nested folders)."""
    return f"papers/{flat_name(doi)}.{ext}"
