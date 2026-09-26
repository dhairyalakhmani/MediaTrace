import os
from dataclasses import dataclass

from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

load_dotenv()


@dataclass(frozen=True)
class Settings:
    pg_dsn: str
    pg: dict  # the individual connection parts (host, port, dbname, user, password, sslmode)
    s3_endpoint: str | None
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_region: str
    contact_email: str | None
    core_api_key: str | None
    wiley_tdm_token: str | None
    elsevier_api_key: str | None
    elsevier_insttoken: str | None
    grobid_url: str


def pg_params(prefix: str, defaults: dict | None = None) -> dict:
    """Read <PREFIX>_HOST/_PORT/_DB/_USER/_PASSWORD/_SSLMODE from the environment."""
    d = defaults or {}
    env = os.environ.get
    return {"host": env(f"{prefix}_HOST", d.get("host")), "port": env(f"{prefix}_PORT", d.get("port", "5432")),
            "dbname": env(f"{prefix}_DB", d.get("dbname")), "user": env(f"{prefix}_USER", d.get("user")),
            "password": env(f"{prefix}_PASSWORD", d.get("password")),
            "sslmode": env(f"{prefix}_SSLMODE", d.get("sslmode", "require"))}


def load() -> Settings:
    env = os.environ.get
    pg = pg_params("POSTGRES", {"host": "localhost", "port": "5432", "dbname": "lemkb", "user": "lemkb",
                                "password": "lemkb", "sslmode": "prefer"})
    return Settings(
        pg_dsn=make_conninfo(**pg),  # quotes safely, so any character works in the password
        pg=pg,
        s3_endpoint=env("S3_ENDPOINT") or None,
        s3_access_key=env("S3_ACCESS_KEY", ""),
        s3_secret_key=env("S3_SECRET_KEY", ""),
        s3_bucket=env("S3_BUCKET", "lemkb"),
        s3_region=env("S3_REGION", "us-east-1"),
        contact_email=env("CONTACT_EMAIL") or None,
        core_api_key=env("CORE_API_KEY") or None,
        wiley_tdm_token=env("WILEY_TDM_TOKEN") or None,
        elsevier_api_key=env("ELSEVIER_API_KEY") or None,
        elsevier_insttoken=env("ELSEVIER_INSTTOKEN") or None,
        grobid_url=env("GROBID_URL", "http://localhost:8070").rstrip("/"),
    )


settings = load()
