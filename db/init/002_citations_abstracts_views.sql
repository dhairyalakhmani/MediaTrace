-- 002: verified citations, abstracts / evidence levels, typed MediaAssist views.
-- Idempotent: safe to run on a fresh database or re-run on an existing one (lemkb migrate).

-- ---------------------------------------------------------------------------
-- Papers: abstracts + evidence level
-- ---------------------------------------------------------------------------
ALTER TABLE papers ADD COLUMN IF NOT EXISTS pmcid TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS manual_url TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS pmid TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS abstract TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS abstract_source TEXT;
-- A person confirmed this file is the right paper (e.g. they uploaded it themselves).
ALTER TABLE papers ADD COLUMN IF NOT EXISTS user_confirmed BOOLEAN NOT NULL DEFAULT FALSE;

-- What an agent can actually read for this paper. Always consistent with status/abstract.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS evidence_level TEXT
    GENERATED ALWAYS AS (
        CASE WHEN status = 'downloaded' THEN 'full_text'
             WHEN abstract IS NOT NULL  THEN 'abstract'
             ELSE 'metadata_only' END
    ) STORED;

-- ---------------------------------------------------------------------------
-- Citations: every MediaAssist row -> the source it cites, with verification.
-- MediaAssist's citation text sometimes carries a wrong DOI (e.g. spreadsheet
-- drag-fill), so a DOI is only trusted once its registered title matches the
-- cited title.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS row_citations (
    id               BIGSERIAL PRIMARY KEY,
    row_id           BIGINT NOT NULL REFERENCES mediaassist_rows (id) ON DELETE CASCADE,
    citation_text    TEXT,
    citation_url     TEXT,
    source_kind      TEXT NOT NULL DEFAULT 'unresolved'
                     CHECK (source_kind IN ('paper', 'non_doi', 'unresolved')),
    doi              TEXT,                       -- chosen DOI (NULL for non_doi / unresolved)
    resolved_via     TEXT,                       -- citation_text | link_doi | link_pmid | crossref_search
    title_match      REAL,                       -- 0..1 overlap of registered title with citation text
    verified         BOOLEAN NOT NULL DEFAULT FALSE,
    candidates       JSONB,                      -- every DOI considered, with its registered title + score
    issue            TEXT,                       -- human-readable problem, e.g. DOI in text points to another paper
    resolved_at      TIMESTAMPTZ,
    UNIQUE (row_id)
);

CREATE INDEX IF NOT EXISTS row_citations_doi_idx ON row_citations (doi);

-- A fact can cite several sources ("A | B | C" in MediaAssist): one row per source.
ALTER TABLE row_citations ADD COLUMN IF NOT EXISTS position INT NOT NULL DEFAULT 0;
ALTER TABLE row_citations DROP CONSTRAINT IF EXISTS row_citations_row_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS row_citations_row_pos_uniq ON row_citations (row_id, position);

-- Facts -> evidence, now through verified citations.
DROP VIEW IF EXISTS fact_evidence;
CREATE VIEW fact_evidence AS
SELECT r.id             AS row_id,
       r.query_type,
       r.data,
       c.position       AS citation_position,
       c.source_kind,
       c.doi,
       c.verified       AS citation_verified,
       c.issue          AS citation_issue,
       c.citation_text,
       c.citation_url,
       p.status         AS paper_status,
       p.evidence_level,
       p.title,
       p.s3_bucket,
       p.s3_key
FROM mediaassist_rows r
LEFT JOIN row_citations c ON c.row_id = r.id
LEFT JOIN papers p        ON p.doi = c.doi;

-- Missing papers ranked by how many facts depend on them: fix the top of this list first.
CREATE OR REPLACE VIEW gap_priority AS
SELECT p.doi, p.status, p.evidence_level, p.title, p.journal, p.publisher, p.year,
       p.manual_url, count(DISTINCT c.row_id) AS facts_citing
FROM papers p
LEFT JOIN row_citations c ON c.doi = p.doi
WHERE p.status <> 'downloaded'
GROUP BY p.doi
ORDER BY facts_citing DESC, p.doi;

-- ---------------------------------------------------------------------------
-- Typed views over the raw rows. Raw rows stay the source of truth; these only
-- rename columns consistently and parse numbers. Unparseable values stay NULL
-- in the numeric column while the original text is kept alongside.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION try_numeric(t TEXT) RETURNS NUMERIC
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    -- thousands separators ('41,300') and non-breaking spaces must not cut the number short
    t := regexp_replace(replace(replace(t, '−', '-'), chr(160), ' '), '([0-9]),([0-9]{3})', '\1\2', 'g');
    RETURN substring(t FROM '^\s*(-?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)')::NUMERIC;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

DROP VIEW IF EXISTS v_concentration;
CREATE VIEW v_concentration AS
SELECT r.id AS row_id,
       r.data->>'Component Name'                   AS component,
       r.data->>'Medium'                           AS medium,
       try_numeric(r.data->>'Concentration (mM)')  AS concentration_mm,
       (SELECT array_agg(x.doi ORDER BY x.position) FILTER (WHERE x.doi IS NOT NULL) FROM row_citations x WHERE x.row_id = r.id) AS all_dois,
       c.doi, c.verified AS citation_verified, c.citation_text, c.citation_url
FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id AND c.position = 0
WHERE r.query_type = 'concentration';

DROP VIEW IF EXISTS v_solubility;
CREATE VIEW v_solubility AS
SELECT r.id AS row_id,
       r.data->>'Component Name'                            AS component,
       r.data->>'Temperature (°C)'                          AS temperature_c_raw,
       try_numeric(r.data->>'Temperature (°C)')             AS temperature_c,
       r.data->>'Aqueous Solubility'                        AS solubility_raw,
       try_numeric(r.data->>'Aqueous Solubility')           AS solubility_value,
       nullif(trim(regexp_replace(replace(r.data->>'Aqueous Solubility', chr(160), ' '), '^\s*-?[0-9][0-9.,eE+-]*', '')), '') AS solubility_unit,
       (SELECT array_agg(x.doi ORDER BY x.position) FILTER (WHERE x.doi IS NOT NULL) FROM row_citations x WHERE x.row_id = r.id) AS all_dois,
       c.source_kind, c.doi, c.verified AS citation_verified, c.citation_text, c.citation_url
FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id AND c.position = 0
WHERE r.query_type = 'solubility';

DROP VIEW IF EXISTS v_stability;
CREATE VIEW v_stability AS
SELECT r.id AS row_id,
       r.data->>'Component Name'              AS component,
       r.data->>'Temperature (°C)'            AS temperature_raw,   -- often a range, e.g. 4°C-8°C
       try_numeric(r.data->>'pH')             AS ph,
       r.data->>'Stability Measure'           AS stability_measure,
       try_numeric(r.data->>'Stability')      AS stability_value,
       (SELECT array_agg(x.doi ORDER BY x.position) FILTER (WHERE x.doi IS NOT NULL) FROM row_citations x WHERE x.row_id = r.id) AS all_dois,
       c.doi, c.verified AS citation_verified, c.citation_text, c.citation_url
FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id AND c.position = 0
WHERE r.query_type = 'stability';

DROP VIEW IF EXISTS v_uptake_rate;
CREATE VIEW v_uptake_rate AS
SELECT r.id AS row_id,
       r.data->>'Component Name'                                         AS component,
       r.data->>'Medium/Condition in which uptake rate is measured'      AS condition,
       try_numeric(r.data->>'Concentration administered (mM)')           AS concentration_administered_mm,
       r.data->>'Specific Uptake Rate'                                   AS uptake_rate_raw,
       try_numeric(r.data->>'Specific Uptake Rate in pmol/cell/day')     AS uptake_rate_pmol_per_cell_day,
       r.data->>'Cell Type'                                              AS cell_type,
       try_numeric(r.data->>'Year')::INT                                 AS year,
       (SELECT array_agg(x.doi ORDER BY x.position) FILTER (WHERE x.doi IS NOT NULL) FROM row_citations x WHERE x.row_id = r.id) AS all_dois,
       c.doi, c.verified AS citation_verified, c.issue AS citation_issue, c.citation_text, c.citation_url
FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id AND c.position = 0
WHERE r.query_type = 'uptake_rate';

DROP VIEW IF EXISTS v_transporter;
CREATE VIEW v_transporter AS
SELECT r.id AS row_id,
       r.data->>'Component Name'            AS component,
       r.data->>'Transporter'               AS transporter,
       r.data->>'Transporter Aliases'       AS transporter_aliases,
       r.data->>'Mechanism of Transport'    AS mechanism,
       r.data->>'Transporter Gene link'     AS uniprot_url,
       r.data->>'Cell Type'                 AS cell_type,
       (SELECT array_agg(x.doi ORDER BY x.position) FILTER (WHERE x.doi IS NOT NULL) FROM row_citations x WHERE x.row_id = r.id) AS all_dois,
       c.doi, c.verified AS citation_verified, c.citation_text, c.citation_url
FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id AND c.position = 0
WHERE r.query_type = 'transporter';

DROP VIEW IF EXISTS v_shared_transporter;
CREATE VIEW v_shared_transporter AS
SELECT r.id AS row_id,
       r.data->>'Selected Component'        AS component_a,
       r.data->>'Other Component'           AS component_b,
       r.data->>'Shared Transporter'        AS transporter,
       r.data->>'Transporter Aliases'       AS transporter_aliases,
       r.data->>'Mechanism'                 AS mechanism,
       r.data->>'Transporter Gene link'     AS uniprot_url,
       r.data->>'Cell Type'                 AS cell_type,
       (SELECT array_agg(x.doi ORDER BY x.position) FILTER (WHERE x.doi IS NOT NULL) FROM row_citations x WHERE x.row_id = r.id) AS all_dois,
       c.doi, c.verified AS citation_verified, c.citation_text, c.citation_url
FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id AND c.position = 0
WHERE r.query_type = 'shared_transporter';

-- MediaAssist uses two header styles for co-dependency exports.
DROP VIEW IF EXISTS v_codependency;
CREATE VIEW v_codependency AS
SELECT r.id AS row_id,
       COALESCE(r.data->>'Selected component', r.data->>'Component 1')  AS component_a,
       COALESCE(r.data->>'Other component',    r.data->>'Component 2')  AS component_b,
       r.data->>'Type of co-dependency'     AS codependency_type,
       r.data->>'Cell Type'                 AS cell_type,
       r.data->>'Medium'                    AS medium,
       r.data->>'Summary'                   AS summary,
       (SELECT array_agg(x.doi ORDER BY x.position) FILTER (WHERE x.doi IS NOT NULL) FROM row_citations x WHERE x.row_id = r.id) AS all_dois,
       c.doi, c.verified AS citation_verified, c.citation_text, c.citation_url
FROM mediaassist_rows r LEFT JOIN row_citations c ON c.row_id = r.id AND c.position = 0
WHERE r.query_type = 'codependency';
