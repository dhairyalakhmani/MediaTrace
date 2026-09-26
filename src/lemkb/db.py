import psycopg
from psycopg.rows import dict_row

from .config import settings


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.pg_dsn, row_factory=dict_row, autocommit=True)


def register_dois(conn: psycopg.Connection, dois: list[str]) -> int:
    """Insert DOIs as 'pending' papers; existing rows are left untouched. Returns # new."""
    if not dois:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO papers (doi) SELECT unnest(%s::text[]) ON CONFLICT (doi) DO NOTHING",
            (list(dict.fromkeys(dois)),),
        )
        return cur.rowcount
