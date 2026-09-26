# MediaTrace

**Every cell-culture media fact, traced to the paper that proves it.**

MediaTrace is a knowledge base for designing cell-culture media. It gathers facts about
media components (concentrations in commercial media, aqueous solubility, stability,
specific uptake rates, transporters and component–component interactions), verifies where
each fact came from, and makes the underlying research papers searchable. People, programs
and AI assistants can all query it, and every answer comes with its source.

> The working implementation lives on the [`lemnisca`](../../tree/lemnisca) branch.
> This `main` branch holds the project overview and future work.

---

## Why it exists

Knowledge about media ingredients is spread across thousands of papers, and curated tables
aren't always right. While building MediaTrace we found hundreds of citations whose DOI
pointed to an unrelated paper (for example, sequential DOIs from a spreadsheet drag-fill),
and supplementary tables stored in place of the articles themselves. A value you can't
trace back to its source can't be trusted for experimental design.

MediaTrace treats **provenance as a first-class feature**: nothing enters the knowledge base
without a verifiable link to its evidence.

## What it does

1. **Collects** curated media data from its source, keeping every original export as proof.
2. **Verifies citations.** Each cited DOI is checked against the title registered with
   Crossref. Wrong or missing DOIs are corrected from links, PubMed IDs, bibliographic search
   or papers already confirmed in the library, and a fact can cite several papers.
3. **Retrieves papers** from legal open-access sources (Europe PMC, Unpaywall, OpenAlex,
   PubMed Central). Papers obtained by hand go through an inbox that checks each file is the
   right paper before storing it.
4. **Reads papers** into citable passages with section and page numbers, full-text indexed.
5. **Answers questions**: through a web app, a REST API and an MCP server for AI assistants,
   including open-ended read-only queries and charts. For example: *"plot stability against
   pH for arginine at each storage temperature"*.

## Trust guarantees

| Guarantee | How |
|---|---|
| Every fact has a source | Citations are verified against registered titles; unverifiable ones are flagged, never hidden |
| Every file is the real paper | Title checks on every download and upload; SHA-256 fingerprints on every stored file |
| Answers state their evidence level | `full_text`, `abstract` or `metadata_only`, reported with each answer |
| AI can read but never change data | Read-only database role and read-only transactions for all AI queries |
| Continuous checking | Random end-to-end validation: source, DOI, file integrity, file identity and query path |

## Architecture

```
 Source tables ──▶ Ingest ──▶ Citation verification ──▶ Paper retrieval ──▶ Text extraction
                    │                 │                        │                   │
                    ▼                 ▼                        ▼                   ▼
             ┌──────────────────────────────────────────────────────────────────────────┐
             │  Vault (object storage)   originals, papers, parsed text, all hashed     │
             │  Catalog (PostgreSQL)     facts, citations, papers, passages, access     │
             └──────────────────────────────────────────────────────────────────────────┘
                                   │
            ┌──────────────────────┼─────────────────────────┐
            ▼                      ▼                         ▼
        Web app               REST API              MCP server (local or hosted,
   (browse, upload,       (for programs)           per-user tokens) for AI clients
    manage access)                                  such as Claude Code / Desktop
```

## Built with

Python · PostgreSQL · object storage (S3-compatible) · GROBID · FastAPI ·
Model Context Protocol · matplotlib · Docker · AWS (RDS, S3, Lightsail) · Caddy

## Current scope

- ~2,700 facts across 39 media components and 7 data types
- ~2,800 verified citations linking to ~270 research papers
- ~170 papers with full text, split into ~7,000 searchable passages
- 10 MCP tools, including SQL analysis and chart generation

## Roadmap

- Complete full-text coverage for the remaining papers
- Scheduled re-checks for newly open-access papers and retractions
- Semantic (embedding) search to support retrieval-augmented generation
- Unit harmonisation across sources and richer experimental-condition metadata
- Team deployment with single sign-on
