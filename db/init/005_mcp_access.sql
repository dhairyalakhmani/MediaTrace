-- 005: who may use the hosted MCP server. Managed from the web app's Access page.
-- Tokens are stored only as SHA-256 hashes. The hosted server's read-only login can't
-- read this table; it can only call mcp_check_token(), so query_data can't expose it.

CREATE TABLE IF NOT EXISTS mcp_users (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ
);
-- one active token per name
CREATE UNIQUE INDEX IF NOT EXISTS mcp_users_active_name ON mcp_users (lower(name)) WHERE revoked_at IS NULL;

-- Returns the user's name for a valid, unrevoked token hash (NULL otherwise) and records use.
CREATE OR REPLACE FUNCTION mcp_check_token(p_hash TEXT) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    v_name TEXT;
BEGIN
    UPDATE mcp_users SET last_used_at = now()
     WHERE token_hash = p_hash AND revoked_at IS NULL
       AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')
    RETURNING name INTO v_name;
    IF v_name IS NULL THEN
        SELECT name INTO v_name FROM mcp_users WHERE token_hash = p_hash AND revoked_at IS NULL;
    END IF;
    RETURN v_name;
END $$;

REVOKE ALL ON FUNCTION mcp_check_token(TEXT) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lemkb_reader') THEN
        REVOKE ALL ON mcp_users FROM lemkb_reader;       -- default privileges had granted SELECT
        GRANT EXECUTE ON FUNCTION mcp_check_token(TEXT) TO lemkb_reader;
    END IF;
END $$;
