# Lemnisca Media Knowledge Base: data foundation

One trusted place for media-component research data:

- **MediaAssist tables** (formulations, solubility, stability, uptake, transporters, co-dependencies) → Postgres
- **Every cited paper** (by DOI) → full text in S3 (MinIO locally), fetched automatically from legal sources
- **Every fact linked to its paper**, and every paper has an explicit status, so we always know what we have and what's missing.

- **Readable text:** every paper split into citable passages (`<doi>#p<seq>`) with section and page.
- **Access:** an MCP server for AI agents and a REST API for apps, both returning provenance with every answer.

| Layer | What | Where |
|---|---|---|
| 1. Vault | original PDFs/XML and MediaAssist exports, untouched and hashed | S3 (MinIO) `papers/`, `mediaassist/raw/` |
| 2. Catalog | papers, facts, verified citations, typed views | Postgres |
| 3. Readable text | passages (title / abstract / body / caption / table) + JSON export | Postgres `passages`, S3 `parsed/` |
| 4. Access | MCP tools, REST API, SQL, bulk export | `lemkb serve-mcp`, `lemkb serve-api` |

```
MediaAssist table ──► S3: mediaassist/raw/...   (untouched original file = proof)
        │         └─► Postgres: mediaassist_rows (each row as-is)
        │
        └── resolve: citation text + link → candidate DOIs → check against registered titles
                   └─► row_citations (verified DOI, or flagged issue) ──► papers (status: pending)
                        │  fetch: Europe PMC → Unpaywall → OpenAlex → PMC cloud → Semantic Scholar → CORE
                        ▼
                  S3: papers/<doi>.pdf|.xml   +  papers row: source URL, sha256, license, status
```

## Using it

**Double-click `start.bat`.** It starts the database, file storage and PDF parser (Docker Desktop must be running), then opens the app in your browser at http://127.0.0.1:8000.

| Screen | What you do there |
|---|---|
| **Dashboard** | See coverage; work down the list of missing papers, most important first (Open publisher page → download → Upload PDF, or drop any PDFs on the drop area); re-check missing papers; back up |
| **Import** | Drop MediaAssist exports and press **Process**: files are stored, citations verified, papers downloaded, text extracted, with live progress |
| **Facts** | Filter by component, type or anything else; click a fact for its citation, DOI check, supporting passages and the PDF |
| **Papers** | Every cited paper, with status, abstract, full text, and Open PDF / Upload |
| **Search text** | Full-text search across all passages |

Uploaded PDFs are checked against the paper's real title: a wrong file is refused (you can override that deliberately).

The commands below do the same things from a terminal, for automation.

## Where the data lives (since 2026-09-26)

| What | Where |
|---|---|
| Postgres (facts, citations, paper catalog, passages) | **AWS RDS** `lemkb-db` (ap-south-1, PostgreSQL 18, SSL required, automated backups 7 days) |
| Files (papers, parsed text, MediaAssist exports) | **AWS S3** `lemnisca-media-kb-bucket` (ap-south-1) |
| Local Docker | GROBID (PDF parser) and PostgreSQL 18 client tools. The local Postgres and MinIO still hold the pre-migration copy as a fallback, but are **no longer updated** |

Connection settings are in `.env` (`POSTGRES_*` points at RDS; `S3_*` points at AWS, with credentials from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`).

- **RDS firewall:** the security group `lemkb-db-sg` only admits the IP it was created from. From a new network, add your IP (RDS → lemkb-db → Connectivity & security → security group → Inbound rules → PostgreSQL, My IP), or `db-check` will time out.
- **Migration commands** (already run): `db-check`, `db-migrate` (copies, then verifies 17 row-count / content-fingerprint / view checks), `db-switch` (`--local` to go back); `s3-check`, `s3-migrate`.

## Setup (once)

```bash
docker compose up -d                      # Postgres :5432, MinIO :9000 (console :9001), GROBID :8070
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env                      # then set CONTACT_EMAIL (needed for Unpaywall)
```

Run commands from the project root with `PYTHONPATH=src` (bash) or `$env:PYTHONPATH="src"` (PowerShell):

```bash
python -m lemkb check                                  # Postgres + S3 reachable?
python -m lemkb migrate                                # apply db/init/*.sql to an existing database
python -m lemkb ingest-dir "data/mediaAssist Samples"  # load every export; type read from filename; resolves citations
python -m lemkb ingest table.csv --type solubility     # load one table with an explicit type
python -m lemkb resolve [--all]                        # (re)verify citations -> DOIs
python -m lemkb enrich                                 # backfill abstracts for papers that lack one
python -m lemkb load-dois dois.txt                     # or register DOIs directly
python -m lemkb fetch --workers 4                      # download every pending paper
python -m lemkb report --gaps data/gap_list.csv        # coverage + list of what's missing
python -m lemkb get 10.1038/nbt.1614                   # metadata + temporary download link
python -m lemkb upload 10.1126/science.1098641 x.pdf   # attach a paper a person downloaded
python -m lemkb inbox                                  # match + store every PDF dropped in data/inbox/
python -m lemkb fetch --recheck-missing                # retry every missing paper (e.g. after adding a TDM token)
python -m lemkb parse                                  # papers -> passages (only new/changed papers)
python -m lemkb serve-api                              # REST API, docs at http://127.0.0.1:8000/docs
python -m lemkb serve-mcp                              # MCP server (stdio); --http for a shared server
python -m lemkb backup                                 # Postgres dump + all S3 files + verified manifest
python -m lemkb harvest config/mediaassist_queries.json --dry-run
```

Typical run after new exports arrive: `ingest-dir` → `fetch` → `parse` → `backup`.

All commands are safe to re-run: files are de-duplicated by SHA-256, and `fetch` only processes pending papers (add `--retry-failed` to retry failed ones).

## Citation verification

MediaAssist's citation text sometimes carries a wrong DOI. In the sample uptake-rate export, 19 of 45 rows cite DOIs that belong to unrelated papers (a spreadsheet drag-fill: `bit.26794` → `bit.26795`, `bit.26796`, …). So no DOI is trusted until its registered title matches the cited title:

1. Candidates come from the citation text, a DOI in the link, a PubMed ID in the link (converted to a DOI via Europe PMC), and, if there's nothing else, a Crossref bibliographic search.
2. Each candidate's registered title (Crossref) is scored against the citation text. The best one wins; it counts as `verified` at a score of at least 0.75 (0.9 for search hits).
3. Every candidate, its score and any disagreement are stored in `row_citations`. Only the chosen DOIs become papers.

Sources without a DOI (handbooks, PubChem pages) are kept as `source_kind = 'non_doi'` with their link.

## Evidence levels: what an agent can read

`papers.evidence_level` is computed from status and abstract, so it's always consistent:

| level | agent gets |
|---|---|
| `full_text` | the PDF/XML in S3 |
| `abstract` | title, abstract, journal and year (from Europe PMC / Crossref / OpenAlex / Semantic Scholar) |
| `metadata_only` | title, journal and year |

Agents and RAG should state the level behind each answer. When a missing paper is uploaded later, its level becomes `full_text` automatically.

## Paper status: the guarantee

| status | meaning | action |
|---|---|---|
| `downloaded` | file in S3; source URL, hash and license recorded | none |
| `pending_manual` | a free copy exists, but the site blocks automated download | open `manual_url` in a browser, then `lemkb upload` |
| `not_available` | no legal open copy anywhere | library access / publisher TDM / ask the authors, then `upload` |
| `download_failed` | temporary errors | `fetch --retry-failed` |
| `invalid_doi` | DOI doesn't exist in Crossref or any index | fix the source data |

`fetch_attempts` logs every try (source, URL, HTTP code), so "why don't we have this paper?" always has an answer.

## Closing the gaps

Missing papers are ranked in `gap_priority` by how many facts depend on them. There are two routes to fill them:

1. **Publisher TDM APIs** (automatic once licensed). Wiley and Elsevier connectors are built in and switched on by `WILEY_TDM_TOKEN` / `ELSEVIER_API_KEY` in `.env`. Their free self-service tokens are for **non-commercial** use, so for Lemnisca they need to be issued under a commercial agreement. Then run `fetch --recheck-missing`.
2. **Manual inbox.** Drop PDFs obtained through library access, from the authors, or by purchase into `data/inbox/` (any filename) and run `lemkb inbox`. Each file is identified by the DOI in its filename or text, or by its title. It's stored only if the paper's registered title appears in the file. A file that can't be matched is left in place, never guessed; a file named with the wrong DOI is caught.

## Readable text (Layer 3)

`lemkb parse` turns every paper into ordered passages:
- **PDF:** GROBID (sections, paragraphs, figure/table captions, page numbers). Falls back to plain page text if GROBID is unavailable.
- **JATS XML** (PMC): parsed directly.
- **Papers we don't have:** title + abstract, so they're still searchable (evidence level `abstract`).

A paper is re-parsed automatically when its file changes, e.g. after an inbox upload replaces an abstract with the full PDF. Passages are full-text indexed in Postgres, and exported as JSON to S3 `parsed/<doi>.json` for any future RAG pipeline.

## Access for agents and apps (Layer 4)

**MCP tools:** `search_facts`, `list_components`, `evidence_for_fact` (the fact + the passages of its paper that support it), `search_passages`, `get_paper`, `get_passages`, `coverage`, plus open-ended analysis:
- `describe_data`: the analysis views (`v_solubility`, `v_stability`, `v_uptake_rate`, …) and their columns
- `query_data`: one read-only SQL `SELECT` (15 s timeout, 500-row cap). It runs in a `READ ONLY` transaction, so the database refuses any write.
- `plot`: a scatter, line or bar chart of any two columns (optionally one series per group), returned as an image with the plotted rows and source DOIs. For example: *"plot stability against pH for asparagine at each storage temperature"*.

Every fact lists all its cited sources (`citations`): a fact can cite several papers, and each is linked and checked separately. The server tells agents to cite DOIs and passage IDs, to state the evidence level, and to flag unverified citations.

- **Claude Code:** registered at user scope, so it's available in every folder (`claude mcp get lemnisca-media-kb`). `.mcp.json` also registers it for anyone opening this repo.
- **Claude Desktop:** configured in `%APPDATA%\Claude\claude_desktop_config.json`. Restart Claude Desktop after installing it.
- **Custom agent:** `examples/agent.py` starts the server, gives its tools to Claude (Opus 5, via the Anthropic SDK tool runner and its MCP helpers) and prints a cited answer. It needs `ANTHROPIC_API_KEY` in `.env`: `.venv\Scripts\python examplesgent.py "your question"`.
- **Any other MCP client:** command `.venv/Scripts/python.exe -m lemkb.mcp_server`, env `PYTHONPATH=src`.
- The server needs only `.env` (RDS + S3), not Docker. It runs locally only; don't expose `serve-mcp --http` to the internet, since it has no authentication.

**REST** (under `/api`, interactive docs at `/docs`): `GET /api/facts?component=&query_type=&text=`, `/api/facts/{id}/evidence`, `/api/components`, `/api/passages/search?q=`, `/api/papers`, `/api/papers/{doi}`, `/api/papers/{doi}/passages`, `/api/papers/{doi}/file`, `/api/gaps`, `/api/coverage`. Uploads: `POST /api/inbox?filename=` and `POST /api/papers/{doi}/upload?filename=` with the raw file as the request body. Jobs: `POST /api/import/stage`, `/api/import/run`, `/api/jobs/{fetch,parse,enrich,backup}`, and `GET /api/jobs/current`.

## Hosted MCP server (team access)

**Live since 2026-09-26:** `https://13-126-230-191.sslip.io/mcp`. It's on Lightsail `lemkb-mcp` (micro_3_1, ap-south-1, US$7/mo) with static IP `13.126.230.191` and a Let's Encrypt certificate, renewed automatically by Caddy. Health: `/health`. Status: `python deploy/lightsail_deploy.py status`.

The same MCP server can run on a small AWS Lightsail machine, reachable over HTTPS, with **one access token per person**.

**Security**
- Every `/mcp` request needs `Authorization: Bearer <token>`. Tokens are stored only as SHA-256 hashes in the `mcp_users` table. The server checks them through `mcp_check_token()`, and its read-only login can't read the table itself.
- **Manage people on the web app's Access page:** type a name → **Create token** (you get the token plus ready-to-send Claude Code and Claude Desktop setup), or **Revoke**. Changes reach the hosted server within 30 seconds, with no redeploy. `lemkb mcp-token add|list|revoke` does the same from a terminal.
- The server uses a **read-only database login** (`lemkb_reader`), so the database refuses any write.
- RDS only accepts the server's static IP and your own.

**One-time setup by an AWS admin:** attach `deploy/iam-policy.json` to the IAM user whose keys are in `.env`. Then:
```bash
python -m lemkb mcp-token add <your-name>        # prints your token once; keep it
python deploy/lightsail_deploy.py plan           # checks permissions, changes nothing
python deploy/lightsail_deploy.py deploy         # creates the server and prints its URL
python deploy/lightsail_deploy.py status
```
- The server is about US$7 a month (1 GB RAM, ap-south-1).
- The URL is `https://<static-ip-with-dashes>.sslip.io/mcp`, with an automatic Let's Encrypt certificate via Caddy. Swap in a company domain later.
- Re-run `deploy` only to ship new code; access changes never need it.

**Connect Claude Code** (any machine):
```bash
claude mcp add --scope user --transport http lemnisca-media-kb-remote https://<host>/mcp --header "Authorization: Bearer <token>"
```

**Connect Claude Desktop** (any machine with Node.js). Add this to `claude_desktop_config.json`, then restart Claude Desktop:
```json
{ "mcpServers": { "lemnisca-media-kb-remote": {
    "command": "npx",
    "args": ["-y", "mcp-remote@latest", "https://<host>/mcp", "--header", "Authorization:${AUTH_HEADER}"],
    "env": { "AUTH_HEADER": "Bearer <token>" } } } }
```
Claude Desktop uses the `mcp-remote` bridge because its config can't send headers itself.

**Tested:** the server setup (`deploy/bootstrap.sh`) was rehearsed in an Ubuntu 24.04 container. There, all 10 tools worked over authenticated HTTP against RDS/S3 with the read-only login, including `plot` images and S3 links; requests without a token got 401; and the `mcp-remote` bridge worked the way Claude Desktop runs it. Not tested yet (needs the real server): systemd auto-start and the HTTPS certificate.

## Backup

`lemkb backup` writes `backups/<timestamp>/` containing a `pg_dump` of Postgres, a copy of every S3 object, and a `manifest.json` that re-checks every paper file against its catalog hash. To restore: `pg_restore -d lemkb postgres.dump` (inside the postgres container), then upload `s3/` back to the bucket. Keep backups off this laptop (a network drive or cloud storage) until the system moves to AWS.

## Useful queries

```sql
SELECT * FROM coverage_report;                                    -- how complete are we?
SELECT * FROM gap_priority;                                       -- missing papers, most-cited first
SELECT * FROM v_uptake_rate WHERE component = 'Alanine';          -- typed views: v_concentration, v_solubility,
                                                                  --   v_stability, v_uptake_rate, v_transporter,
                                                                  --   v_shared_transporter, v_codependency
SELECT row_id, issue FROM row_citations WHERE issue IS NOT NULL;  -- citation problems found in the source data
SELECT * FROM passage_index WHERE doi = '10.1016/j.jtemb.2016.12.016' ORDER BY seq;  -- a paper's text
SELECT * FROM fact_evidence WHERE data->>'Component' ILIKE '%glutamine%';  -- facts + their papers
SELECT doi, manual_url FROM papers WHERE status = 'pending_manual';        -- to-do list for a person
```

## MediaAssist harvesting

**Import → "Harvest MediaAssist"** in the interface (or `python -m lemkb harvest-all`) downloads every export for every component and query type, using the site's own Excel download endpoints: the same files its Download buttons produce.

- For each query type, it lists the components, then each component's options (media, temperatures, pH, mechanisms, partner components), and uses the site's "all" option wherever one is offered.
- Shared transporters are fetched once per unordered component pair; co-dependencies once per component with type "all".
- Polite: one request at a time, 1.5 s apart, retries with backoff, and it stops after repeated failures.
- Resumable: requests that already succeeded are skipped on re-runs.
- A fact is stored once, however many exports contain it (unique index on the row's content). Re-downloaded Excel files differ byte-wise, so file hashes alone can't catch repeats.
- Subset: `harvest-all --types solubility,stability --components Alanine,Glutamine`.

MediaAssist states that commercial use requires permission from CSIR-NCL (director@ncl.res.in).

## Notes

- `data/sample_dois.txt`: 10 real DOIs for smoke tests of the fetcher alone.
- CORE: its free API key is for academic use only, so leave `CORE_API_KEY` empty for Lemnisca unless a commercial arrangement is made. Storing results in S3 doesn't change the license terms.
- The PMC cloud source is NCBI's public AWS dataset (`pmc-oa-opendata`). Each paper's `license_code` (CC BY, TDM, …) is stored in `papers.license`.
- Typed tables (e.g. `solubility(component, value, unit, temp, pH, doi)`) will be added as views over `mediaassist_rows` once the real column layouts are confirmed.
- Moving to AWS: point the `.env` values at RDS and S3 (leave `S3_ENDPOINT` empty). No code changes are needed.
