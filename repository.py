"""Persistence for the extracted Company state.

Two implementations behind a thin Protocol:

- InMemoryRepo: process-local dict. Used in local dev (no DATABASE_URL),
  preserves the prior behavior exactly.
- PostgresRepo: a single `companies` table with a JSONB payload column.
  Switches on automatically when DATABASE_URL is set (Railway's Postgres
  plugin sets this env var). Survives redeploys, which the in-memory dict
  did not — overrides a reviewer makes now persist past container restart.

The schema is intentionally one table with one JSONB blob: the override
audit trail is already encoded inside the Company payload (every LineItem
carries source=USER_OVERRIDE plus a source_quote when overridden), so we
don't need a separate overrides table to preserve that history.

Failure mode: if PostgresRepo loses its pool mid-request it falls back to
returning None on read and quietly dropping the write. The /extract path
will simply re-run the agent on the next request rather than serve stale
data — an acceptable degradation for a demo that survives a transient DB
hiccup.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional, Protocol

import asyncpg

from schemas import Company


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS companies (
    ticker     TEXT PRIMARY KEY,
    payload    JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class CompanyRepo(Protocol):
    async def get(self, ticker: str) -> Optional[Company]: ...
    async def set(self, ticker: str, company: Company) -> None: ...
    async def close(self) -> None: ...


class InMemoryRepo:
    """Process-local dict — exact behavior of the pre-#5 implementation."""

    def __init__(self) -> None:
        self._store: dict[str, Company] = {}

    async def get(self, ticker: str) -> Optional[Company]:
        return self._store.get(ticker.upper())

    async def set(self, ticker: str, company: Company) -> None:
        self._store[ticker.upper()] = company

    async def close(self) -> None:
        self._store.clear()


class PostgresRepo:
    """Postgres-backed repo. Connect once at lifespan startup; share the pool."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    @staticmethod
    async def _init_conn(conn: asyncpg.Connection) -> None:
        # Make JSONB round-trip as Python dicts rather than the default text.
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=4,
            init=self._init_conn,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA_DDL)

    async def get(self, ticker: str) -> Optional[Company]:
        if self._pool is None:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT payload FROM companies WHERE ticker = $1",
                ticker.upper(),
            )
        if row is None:
            return None
        payload: Any = row["payload"]
        return Company.model_validate(payload)

    async def set(self, ticker: str, company: Company) -> None:
        if self._pool is None:
            return
        payload = company.model_dump(mode="json")
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO companies (ticker, payload, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (ticker) DO UPDATE
                SET payload = EXCLUDED.payload,
                    updated_at = NOW()
                """,
                ticker.upper(),
                payload,
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


async def make_repo() -> CompanyRepo:
    """Pick the right repo based on DATABASE_URL.

    Railway's Postgres plugin sets DATABASE_URL automatically. Local dev
    doesn't have one set, which falls through to InMemoryRepo and keeps
    the prior behavior intact.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return InMemoryRepo()
    repo = PostgresRepo(dsn)
    await repo.connect()
    return repo
