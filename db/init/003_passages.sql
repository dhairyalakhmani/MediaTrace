-- 003: Layer 3, readable text. Every paper we can read becomes ordered passages
-- (title / abstract / body paragraphs / captions) with section and page, so any
-- future RAG system or agent can quote and cite a specific place in a specific paper.

CREATE TABLE IF NOT EXISTS paper_texts (
    doi             TEXT PRIMARY KEY REFERENCES papers (doi) ON DELETE CASCADE,
    source          TEXT NOT NULL,              -- full_text | abstract (what the passages were built from)
    source_sha256   TEXT,                       -- hash of the file parsed; a new upload => re-parse
    parser          TEXT NOT NULL,              -- grobid | jats | pypdf | metadata
    parser_version  TEXT,
    s3_key          TEXT,                       -- parsed JSON export (passages) in S3
    n_passages      INT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('parsed', 'failed')),
    error           TEXT,
    parsed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS passages (
    id              BIGSERIAL PRIMARY KEY,
    doi             TEXT NOT NULL REFERENCES papers (doi) ON DELETE CASCADE,
    seq             INT NOT NULL,               -- reading order within the paper
    kind            TEXT NOT NULL,              -- title | abstract | body | caption | table
    section         TEXT,                       -- e.g. "Materials and methods" (NULL if unknown)
    page            INT,                        -- 1-based page in the PDF (NULL for XML sources)
    text            TEXT NOT NULL,
    tsv             TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(section, '') || ' ' || text)) STORED,
    UNIQUE (doi, seq)
);

CREATE INDEX IF NOT EXISTS passages_doi_idx ON passages (doi);
CREATE INDEX IF NOT EXISTS passages_tsv_idx ON passages USING GIN (tsv);

-- A stable, citable ID for every passage: "<doi>#p<seq>".
CREATE OR REPLACE VIEW passage_index AS
SELECT ps.doi || '#p' || ps.seq AS passage_id,
       ps.doi, ps.seq, ps.kind, ps.section, ps.page, ps.text,
       p.title, p.year, p.journal, p.evidence_level, p.license
FROM passages ps JOIN papers p USING (doi);
