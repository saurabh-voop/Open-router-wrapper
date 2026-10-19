"""Test fixtures: a real Postgres (TEST_DATABASE_URL); OpenRouter is always mocked."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://relay:relay@localhost:5432/relay_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-for-the-test-suite")
os.environ["CATALOG_SYNC_ENABLED"] = "false"
os.environ["ROUTING_CONFIG_PATH"] = str(
    Path(__file__).resolve().parent / "fixtures" / "routing.yaml"
)
os.environ["OPENROUTER_BASE_URL"] = "https://openrouter.test/api/v1"
os.environ["S3_BUCKET"] = "relay-test"
os.environ["S3_ENDPOINT_URL"] = ""
# Never let the suite touch real AWS, whatever credentials the shell has.
for _var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
    os.environ.pop(_var, None)
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "ap-south-1"

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def engine(migrated_db: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DATABASE_URL)
    yield eng
    await eng.dispose()


TABLES = (
    "feedback, tool_runs, routing_decisions, attachments, messages, conversations, "
    "usage_ledger, oauth_tokens, users, models"
)


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture
def s3() -> Iterator[None]:
    """In-memory S3 (moto) with the test bucket."""
    from moto import mock_aws

    from app.services.files import s3_client

    with mock_aws():
        s3_client.cache_clear()
        s3_client().create_bucket(
            Bucket="relay-test", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"}
        )
        yield
        s3_client.cache_clear()


@pytest.fixture
async def client(db: AsyncSession, s3: None) -> AsyncIterator[AsyncClient]:
    """API client. The app uses its own sessions on the (truncated) test database."""
    from app.core.security import login_limiter
    from app.main import create_app

    login_limiter._failures.clear()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
