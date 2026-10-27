"""Real PostgreSQL restart/revision safety; CI supplies an isolated database."""

import asyncio
import os
from uuid import uuid4

import pytest

from eval.cold import FixtureEdgar
from overrides import apply_override
from qualification import current_qualification
from repository import PostgresRepo, RecordConflict, make_repo, payload_id
from decimal import Decimal


def test_production_refuses_ephemeral_storage(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        asyncio.run(make_repo())


def test_closed_database_is_not_a_cache_miss_or_silent_write():
    async def scenario():
        repo = PostgresRepo("unused")
        with pytest.raises(RuntimeError):
            await repo.get("MSFT")
        with pytest.raises(RuntimeError):
            await repo.set("MSFT", FixtureEdgar("MSFT").saved)

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.environ.get("VALUATE_TEST_DATABASE_URL"),
    reason="Dedicated PostgreSQL test database not configured",
)
def test_reviews_versions_candidates_and_evidence_survive_application_restart():
    import asyncpg

    async def scenario():
        dsn = os.environ["VALUATE_TEST_DATABASE_URL"]
        schema = "valuate_test_" + uuid4().hex
        admin = await asyncpg.connect(dsn)
        await admin.execute("CREATE SCHEMA " + schema)
        scoped = dsn + ("&" if "?" in dsn else "?") + "search_path=" + schema
        first = PostgresRepo(scoped)
        second = None
        try:
            await first.connect()
            original = FixtureEdgar("CRM").saved
            await first.set("CRM", original, create_only=True)
            reviewed = apply_override(
                original,
                "income_statement.selling_general_administrative",
                Decimal(17345000000),
                "Exact persisted reviewer note",
            )
            await first.set(
                "CRM",
                reviewed,
                expected_id=payload_id(original.model_dump(mode="json")),
            )
            evidence = {
                "facts": {"cik": original.cik},
                "document": "Pinned source snapshot",
            }
            candidate_id = await first.stage(original, evidence)
            assert await first.get("CRM") == reviewed
            await first.close()
            second = PostgresRepo(scoped)
            await second.connect()
            assert await second.healthy()
            assert await second.get("CRM") == reviewed
            assert await second.candidate(candidate_id) == original
            assert len(await second.versions("CRM")) == 2
            with pytest.raises(RecordConflict):
                await second.set(
                    "CRM",
                    original,
                    expected_id=payload_id(original.model_dump(mode="json")),
                )
            with pytest.raises(RecordConflict):
                await second.set("CRM", original, create_only=True)
            # An unversioned compatibility repair cannot discard a concurrent review.
            await second.set("CRM", original)
            assert await second.get("CRM") == reviewed
            assert (
                current_qualification(await second.get("CRM")).status
                == "review_required"
            )
            async with second._pool.acquire() as conn:
                assert await conn.fetchval("SELECT count(*) FROM filing_evidence") == 1
        finally:
            await first.close()
            if second:
                await second.close()
            await admin.execute("DROP SCHEMA " + schema + " CASCADE")
            await admin.close()

    asyncio.run(scenario())
