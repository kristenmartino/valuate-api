"""Durable active records, immutable revisions, staged candidates and evidence.

Database errors propagate: an unavailable database must never masquerade as a
cache miss or silently discard a reviewer correction. Production requires DB.
"""

from __future__ import annotations
import hashlib
import json
import os
from typing import Optional, Protocol
import asyncpg
from schemas import Company
from tickers import normalize_ticker

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS companies (
 ticker TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS company_versions (
 id TEXT PRIMARY KEY, ticker TEXT NOT NULL, accession TEXT NOT NULL,
 payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS company_versions_ticker ON company_versions(ticker,created_at DESC);
CREATE TABLE IF NOT EXISTS filing_evidence (
 id TEXT PRIMARY KEY, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS extraction_candidates (
 id TEXT PRIMARY KEY, ticker TEXT NOT NULL, payload JSONB NOT NULL,
 evidence_id TEXT REFERENCES filing_evidence(id), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def payload_id(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RecordConflict(RuntimeError):
    """The active revision changed while an operation was in flight."""


def checked_update(old, company, expected_id, create_only):
    if create_only and old is not None:
        raise RecordConflict("A saved record already exists")
    if expected_id is not None and (
        old is None or payload_id(old.model_dump(mode="json")) != expected_id
    ):
        raise RecordConflict("The saved record changed; reload before retrying")
    if old is not None and expected_id is None:
        # Compatibility repairs may run concurrently with a reviewer save.
        # Retain the most recent reviews under the same database transaction.
        from preserve import retain_reviews

        company = retain_reviews(old, company)
    return company


class CompanyRepo(Protocol):
    async def get(self, ticker: str) -> Optional[Company]: ...
    async def set(
        self,
        ticker: str,
        company: Company,
        *,
        expected_id: str | None = None,
        create_only: bool = False,
    ) -> None: ...
    async def stage(self, company: Company, evidence: dict) -> str: ...
    async def candidate(self, candidate_id: str) -> Optional[Company]: ...
    async def versions(self, ticker: str) -> list[dict]: ...
    async def healthy(self) -> bool: ...
    async def close(self) -> None: ...


class InMemoryRepo:
    def __init__(self):
        self._store = {}
        self._versions = {}
        self._candidates = {}
        self._evidence = {}

    async def get(self, ticker):
        return self._store.get(normalize_ticker(ticker))

    async def set(self, ticker, company, *, expected_id=None, create_only=False):
        ticker = normalize_ticker(ticker)
        old = self._store.get(ticker)
        company = checked_update(old, company, expected_id, create_only)
        payload = company.model_dump(mode="json")
        key = payload_id(payload)
        if old:
            previous = old.model_dump(mode="json")
            self._versions.setdefault(ticker, {})[payload_id(previous)] = previous
        self._versions.setdefault(ticker, {})[key] = payload
        self._store[ticker] = company

    async def stage(self, company, evidence):
        payload = company.model_dump(mode="json")
        key = payload_id(payload)
        self._candidates[key] = company
        self._evidence[payload_id(evidence)] = evidence
        return key

    async def candidate(self, key):
        return self._candidates.get(key)

    async def versions(self, ticker):
        return list(self._versions.get(normalize_ticker(ticker), {}).values())

    async def healthy(self):
        return True

    async def close(self):
        self._store.clear()


class PostgresRepo:
    def __init__(self, dsn):
        self._dsn = dsn
        self._pool = None

    @staticmethod
    async def _init_conn(conn):
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    async def connect(self):
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=4, init=self._init_conn
        )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Old and replacement instances can overlap during deployment.
                await conn.execute("SELECT pg_advisory_xact_lock(7609210051)")
                await conn.execute(_SCHEMA_DDL)

    def _require_pool(self):
        if self._pool is None:
            raise RuntimeError("Persistent repository is unavailable")
        return self._pool

    async def get(self, ticker):
        async with self._require_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT payload FROM companies WHERE ticker=$1",
                normalize_ticker(ticker),
            )
        return Company.model_validate(row["payload"]) if row else None

    async def _version(self, conn, ticker, payload):
        await conn.execute(
            "INSERT INTO company_versions(id,ticker,accession,payload) VALUES($1,$2,$3,$4) ON CONFLICT(id) DO NOTHING",
            payload_id(payload),
            ticker,
            payload["periods"][0]["filing_accession"],
            payload,
        )

    async def set(self, ticker, company, *, expected_id=None, create_only=False):
        ticker = normalize_ticker(ticker)
        async with self._require_pool().acquire() as conn:
            async with conn.transaction():
                # A per-ticker advisory transaction lock also covers new rows.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", ticker
                )
                old = await conn.fetchrow(
                    "SELECT payload FROM companies WHERE ticker=$1 FOR UPDATE", ticker
                )
                company = checked_update(
                    Company.model_validate(old["payload"]) if old else None,
                    company,
                    expected_id,
                    create_only,
                )
                payload = company.model_dump(mode="json")
                if old:
                    await self._version(conn, ticker, old["payload"])
                await self._version(conn, ticker, payload)
                await conn.execute(
                    "INSERT INTO companies(ticker,payload) VALUES($1,$2) ON CONFLICT(ticker) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()",
                    ticker,
                    payload,
                )

    async def stage(self, company, evidence):
        payload = company.model_dump(mode="json")
        key = payload_id(payload)
        evidence_id = payload_id(evidence)
        async with self._require_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO filing_evidence(id,payload) VALUES($1,$2) ON CONFLICT(id) DO NOTHING",
                    evidence_id,
                    evidence,
                )
                await conn.execute(
                    "INSERT INTO extraction_candidates(id,ticker,payload,evidence_id) VALUES($1,$2,$3,$4) ON CONFLICT(id) DO NOTHING",
                    key,
                    normalize_ticker(company.ticker),
                    payload,
                    evidence_id,
                )
        return key

    async def candidate(self, key):
        async with self._require_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT payload FROM extraction_candidates WHERE id=$1", key
            )
        return Company.model_validate(row["payload"]) if row else None

    async def versions(self, ticker):
        async with self._require_pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT payload FROM company_versions WHERE ticker=$1 ORDER BY created_at,id",
                normalize_ticker(ticker),
            )
        return [r["payload"] for r in rows]

    async def healthy(self):
        async with self._require_pool().acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


async def make_repo():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        if (
            os.environ.get("RAILWAY_ENVIRONMENT_NAME")
            or os.environ.get("VALUATE_REQUIRE_PERSISTENCE") == "1"
        ):
            raise RuntimeError(
                "DATABASE_URL is required in deployed environments; refusing ephemeral reviewer storage"
            )
        return InMemoryRepo()
    repo = PostgresRepo(dsn)
    await repo.connect()
    return repo
