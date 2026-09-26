-- Lemnisca Media Knowledge Base: foundation schema
-- Layer 1 (vault) lives in S3/MinIO; this schema is Layer 2 (catalog + provenance).

-- ---------------------------------------------------------------------------
-- papers: one row per unique DOI. The status column is the "guarantee":
-- every DOI we know about has an explicit, auditable state.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS papers (
    doi             TEXT PRIMARY KEY,            -- normalized: lowercase, no https://doi.org/ prefix
    pmcid           TEXT,                        -- PubMed Central ID when the paper is in PMC
    title           TEXT,
    year            INT,
    journal         TEXT,
    publisher       TEXT,
    authors         JSONB,
    is_retracted    BOOLEAN,
    is_oa           BOOLEAN,
    oa_status       TEXT,                        -- gold | green | hybrid | bronze | closed (from Unpaywall/OpenAlex)
    license         TEXT,                        -- e.g. cc-by, cc-by-nc, publisher-specific
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'downloaded', 'not_available',
                                      'download_failed', 'pending_manual', 'invalid_doi')),
    source          TEXT,                        -- which service supplied the file (europepmc, unpaywall, ...)
    source_url      TEXT,                        -- exact URL the file was fetched from
    s3_bucket       TEXT,
    s3_key          TEXT,
    sha256          TEXT,
    file_size       BIGINT,
    content_type    TEXT,
    attempts        INT NOT NULL DEFAULT 0,
    last_error      TEXT,
    manual_url      TEXT,                        -- for pending_manual: where a person can get the file in a browser
    metadata        JSONB,                       -- raw metadata responses, kept for provenance
    retrieved_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS papers_status_idx ON papers (status);

-- Every attempt to get a file, successful or not. Answers "why don't we have this paper?"
CREATE TABLE IF NOT EXISTS fetch_attempts (
    id              BIGSERIAL PRIMARY KEY,
    doi             TEXT NOT NULL REFERENCES papers (doi) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    url             TEXT,
    outcome         TEXT NOT NULL,               -- success | no_candidate | http_error | not_pdf | error
    http_status     INT,
    detail          TEXT,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fetch_attempts_doi_idx ON fetch_attempts (doi);

-- ---------------------------------------------------------------------------
-- MediaAssist data. Two steps so nothing is ever lost or mis-mapped:
--   1. mediaassist_downloads: one row per downloaded file (raw file kept in S3)
--   2. mediaassist_rows: every row of every file, stored as-is in JSONB
-- Typed tables (solubility, uptake_rates, ...) are added as views/tables once
-- the exact column layout of each query type is confirmed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mediaassist_downloads (
    id              BIGSERIAL PRIMARY KEY,
    query_type      TEXT NOT NULL,               -- e.g. formulation, solubility, stability, uptake, transporter, codependency
    query_params    JSONB NOT NULL DEFAULT '{}',
    origin          TEXT NOT NULL,               -- 'harvest' (automated) or 'file' (manual upload)
    original_name   TEXT,
    s3_bucket       TEXT NOT NULL,
    s3_key          TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    row_count       INT,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (query_type, sha256)
);

CREATE TABLE IF NOT EXISTS mediaassist_rows (
    id              BIGSERIAL PRIMARY KEY,
    download_id     BIGINT NOT NULL REFERENCES mediaassist_downloads (id) ON DELETE CASCADE,
    query_type      TEXT NOT NULL,
    row_num         INT NOT NULL,
    data            JSONB NOT NULL,              -- the row exactly as MediaAssist gave it
    dois            TEXT[] NOT NULL DEFAULT '{}',-- all DOIs found in the row (normalized)
    UNIQUE (download_id, row_num)
);

CREATE INDEX IF NOT EXISTS mediaassist_rows_type_idx ON mediaassist_rows (query_type);
CREATE INDEX IF NOT EXISTS mediaassist_rows_dois_idx ON mediaassist_rows USING GIN (dois);
CREATE INDEX IF NOT EXISTS mediaassist_rows_data_idx ON mediaassist_rows USING GIN (data jsonb_path_ops);

-- fact_evidence (fact -> verified citation -> paper) is defined in 002.

-- Coverage at a glance.
CREATE OR REPLACE VIEW coverage_report AS
SELECT status,
       count(*)                                        AS papers,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM papers
GROUP BY status
ORDER BY papers DESC;
