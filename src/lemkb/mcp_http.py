"""The MCP server over HTTPS for remote use: token-protected Streamable HTTP.

People are managed on the web app's Access page (or `lemkb mcp-token add|list|revoke`).
Tokens live in the database table mcp_users, as SHA-256 hashes only, so adding or
revoking someone takes effect on the hosted server within ~30 s, with no redeploy.

Every request to /mcp must carry "Authorization: Bearer <token>". The server checks it
with the database function mcp_check_token(), which its read-only login may call but
the table itself stays unreadable to it. /health is open, for health checks.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time

from . import db

log = logging.getLogger("lemkb.mcp_http")
CACHE_SECONDS = 30  # how long a checked token is trusted before re-checking (bounds revocation delay)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# user management (admin side: runs with the admin database login)
# ---------------------------------------------------------------------------

def add_user(name: str) -> str:
    """Create (or replace) a person's token. Returns the token; it is never stored or shown again."""
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    token = "lemkb_" + secrets.token_urlsafe(32)
    with db.connect() as conn, conn.transaction():
        conn.execute("UPDATE mcp_users SET revoked_at = now() WHERE lower(name) = lower(%s) AND revoked_at IS NULL", (name,))
        conn.execute("INSERT INTO mcp_users (name, token_hash) VALUES (%s, %s)", (name, _hash(token)))
    return token


def revoke_user(name: str) -> bool:
    with db.connect() as conn:
        return conn.execute("UPDATE mcp_users SET revoked_at = now() WHERE lower(name) = lower(%s) AND revoked_at IS NULL",
                            (name.strip(),)).rowcount > 0


def list_users(include_revoked: bool = False) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT name, created_at, last_used_at, revoked_at FROM mcp_users
               WHERE %s OR revoked_at IS NULL ORDER BY revoked_at NULLS FIRST, lower(name)""",
            (include_revoked,)).fetchall()]


# ---------------------------------------------------------------------------
# server side
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[str | None, float]] = {}


def _check(digest: str) -> str | None:
    hit = _cache.get(digest)
    if hit and time.monotonic() - hit[1] < CACHE_SECONDS:
        return hit[0]
    with db.connect() as conn:
        user = conn.execute("SELECT mcp_check_token(%s) AS u", (digest,)).fetchone()["u"]
    if len(_cache) > 1000:
        _cache.clear()
    _cache[digest] = (user, time.monotonic())
    return user


class BearerAuth:
    """Pure-ASGI middleware: rejects /mcp requests without a valid bearer token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/health":
            await _respond(send, 200, b'{"status":"ok"}')
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or {}).get(b"authorization", b"").decode()
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        user = None
        if token:
            try:
                user = await asyncio.to_thread(_check, _hash(token))
            except Exception as e:  # database unreachable: fail closed
                log.error("token check failed: %s", e)
                await _respond(send, 503, b'{"error":"authentication temporarily unavailable"}')
                return
        if user is None:
            log.warning("rejected %s %s from %s", scope.get("method"), scope["path"], (scope.get("client") or ["?"])[0])
            await _respond(send, 401, b'{"error":"missing or invalid bearer token"}',
                           [(b"www-authenticate", b'Bearer realm="lemnisca-media-kb"')])
            return
        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)


async def _respond(send, status: int, body: bytes, extra_headers=None):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), *(extra_headers or [])]})
    await send({"type": "http.response.body", "body": body})


def build_app(public_host: str | None = None):
    from mcp.server.transport_security import TransportSecuritySettings

    from .mcp_server import server
    hosts = ["127.0.0.1:*", "localhost:*"] + ([public_host, f"{public_host}:*"] if public_host else [])
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                         allowed_origins=[f"https://{public_host}"] if public_host else [])
    return BearerAuth(server.streamable_http_app(transport_security=security, host="0.0.0.0"))


def serve(host: str = "127.0.0.1", port: int = 8765, public_host: str | None = None) -> None:
    import uvicorn
    uvicorn.run(build_app(public_host), host=host, port=port, log_level="info", proxy_headers=True,
                forwarded_allow_ips="*")


if __name__ == "__main__":  # lightweight entry point for servers (no CLI / pandas imports)
    import argparse
    ap = argparse.ArgumentParser(prog="python -m lemkb.mcp_http")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--public-host")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(a.host, a.port, a.public_host)
