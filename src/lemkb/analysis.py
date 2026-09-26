"""Open-ended analysis for agents: describe the data, run read-only SQL, draw charts.

Fixed tools can't anticipate every question ("plot solubility against temperature for
all amino acids", "which components share the most transporters"), so agents get:

    describe_data()           the analysis views and their columns
    run_query(sql)            one read-only SELECT (15 s timeout, row cap)
    make_plot(sql, x, y, ...) a PNG chart of a query result + the rows and source DOIs

Every query runs in a READ ONLY transaction, so nothing can be modified through it.
"""
from __future__ import annotations

import io
import math
import re
from decimal import Decimal

from . import db

MAX_ROWS = 500
VIEWS = {
    "v_concentration": "component concentration in named media (mM)",
    "v_solubility": "aqueous solubility of components by temperature (value + unit)",
    "v_stability": "component stability under temperature / pH conditions (e.g. day-14 : day-0 ratio)",
    "v_uptake_rate": "specific uptake rates in cell culture (pmol/cell/day; negative = consumption)",
    "v_transporter": "transporters that carry each component (gene, aliases, mechanism, cell type)",
    "v_shared_transporter": "pairs of components that share a transporter",
    "v_codependency": "interactions between two components (type, cell type, medium, summary)",
    "papers": "every cited paper: doi, title, year, journal, status, evidence_level, abstract",
    "passage_index": "paper text passages: passage_id, doi, section, page, text",
    "fact_evidence": "every fact with each of its citations and the cited paper's status",
}
# The READ ONLY transaction is the real guard (the database refuses any write); this only
# gives a clear error early and blocks a few admin functions a read-only transaction allows.
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy)\b"
                        r"|\bpg_(terminate|cancel)_backend|\bpg_reload_conf|\bset_config|\bdblink|\blo_(import|export)", re.I)


def describe_data() -> dict:
    with db.connect() as conn:
        cols = conn.execute(
            """SELECT table_name, column_name, data_type FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = ANY(%s) ORDER BY table_name, ordinal_position""",
            (list(VIEWS),),
        ).fetchall()
    out = {name: {"about": about, "columns": {}} for name, about in VIEWS.items()}
    for c in cols:
        out[c["table_name"]]["columns"][c["column_name"]] = c["data_type"]
    return {
        "views": out,
        "tips": [
            "Component names are capitalised as on MediaAssist, e.g. 'Alanine', 'Glutamine'; use ILIKE for fuzzy matches.",
            "Numeric columns (e.g. solubility_value, temperature_c, stability_value, uptake_rate_pmol_per_cell_day) "
            "are parsed from text; the original text is kept in *_raw columns. Some values are NULL when unparseable.",
            "Every analysis view has doi (first citation) and all_dois; join papers ON papers.doi = v.doi for "
            "title/evidence_level.",
            "Only SELECT / WITH queries are accepted; results are capped at 500 rows.",
        ],
    }


def _clean(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def run_query(sql: str, limit: int = 200) -> dict:
    sql = sql.strip().rstrip(";").strip()
    if ";" in sql:
        raise ValueError("Only one statement is allowed.")
    if not re.match(r"^(select|with)\b", sql, re.I) or _FORBIDDEN.search(sql):
        raise ValueError("Only read-only SELECT / WITH queries are allowed.")
    limit = max(1, min(int(limit), MAX_ROWS))
    with db.connect() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            cur = conn.execute(f"SELECT * FROM ({sql}) AS q LIMIT {limit + 1}")
            rows = cur.fetchall()
            columns = [d.name for d in cur.description]
    truncated = len(rows) > limit
    rows = [{k: _clean(v) for k, v in r.items()} for r in rows[:limit]]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def make_plot(sql: str, x: str, y: str, kind: str = "scatter", group_by: str | None = None,
              title: str | None = None, x_label: str | None = None, y_label: str | None = None,
              log_x: bool = False, log_y: bool = False) -> tuple[bytes, dict]:
    """Chart a query result. kind: scatter | line | bar. group_by: a column whose values become series."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    res = run_query(sql, limit=MAX_ROWS)
    cols = res["columns"]
    for c in (x, y) + ((group_by,) if group_by else ()):
        if c not in cols:
            raise ValueError(f"column {c!r} is not in the query result (columns: {cols})")

    def num(v):
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    series: dict[str, list[tuple]] = {}
    skipped = 0
    for r in res["rows"]:
        yv = num(r[y])
        xv = r[x] if kind == "bar" else num(r[x])
        if yv is None or xv is None:
            skipped += 1
            continue
        series.setdefault(str(r[group_by]) if group_by else y, []).append((xv, yv))
    if not series:
        raise ValueError(f"no plottable rows: {x!r} / {y!r} were empty or non-numeric in all {res['row_count']} rows")

    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=130)
    many = len(series) > 12
    for name, pts in sorted(series.items()):
        if kind == "bar":
            labels = [str(p[0]) for p in pts]
            ax.bar(labels, [p[1] for p in pts], label=None if not group_by else name, alpha=0.85)
        elif kind == "line":
            pts = sorted(pts)
            ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label=name)
        else:
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], label=name, alpha=0.8, s=28)
    if kind == "bar":
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
    if log_x and kind != "bar":
        ax.set_xscale("log")
    if log_y:
        ax.set_yscale("log")
    ax.set_xlabel(x_label or x)
    ax.set_ylabel(y_label or y)
    ax.set_title(title or f"{y} vs {x}")
    ax.grid(alpha=0.3)
    if group_by and not many:
        ax.legend(fontsize=8, loc="best")
    elif group_by:
        ax.legend(fontsize=6, ncol=3, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    fig.text(0.01, 0.01, "Source: Lemnisca Media KB (MediaAssist facts, cited papers)", fontsize=7, color="gray")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)

    dois = sorted({d for r in res["rows"] for d in ([r.get("doi")] + list(r.get("all_dois") or [])) if d})
    return buf.getvalue(), {
        "points_plotted": sum(len(p) for p in series.values()), "rows_skipped_non_numeric": skipped,
        "series": sorted(series), "truncated_at_500_rows": res["truncated"], "source_dois": dois,
        "rows": res["rows"][:60],
    }
