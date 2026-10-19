from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

SPEC_TABLES = {
    "users",
    "conversations",
    "messages",
    "attachments",
    "routing_decisions",
    "feedback",
    "tool_runs",
    "usage_ledger",
    "models",
    "oauth_tokens",
}


async def test_migration_creates_every_spec_table(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert tables >= SPEC_TABLES


async def test_migration_matches_models(engine: AsyncEngine) -> None:
    """Fails if a model changed without a matching Alembic revision."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.models import Base

    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata)
        )
    assert diff == []
