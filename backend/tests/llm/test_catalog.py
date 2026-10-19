from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import respx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.catalog import sync_catalog
from app.llm.openrouter import OpenRouterClient
from app.models import CatalogModel
from app.router.config import load_routing_config
from tests.fakes import BASE_URL, FAKE_MODELS, FAKE_ZDR, catalog_model

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "routing.yaml"


def _mock(models: list[dict[str, object]], zdr: list[dict[str, object]]) -> None:
    respx.get(f"{BASE_URL}/models/user").mock(
        return_value=httpx.Response(200, json={"data": models, "total_count": len(models)})
    )
    respx.get(f"{BASE_URL}/endpoints/zdr").mock(
        return_value=httpx.Response(200, json={"data": zdr})
    )


@respx.mock
async def test_sync_fills_models_table(db: AsyncSession) -> None:
    _mock(FAKE_MODELS, FAKE_ZDR)
    client = OpenRouterClient("k", BASE_URL)
    report = await sync_catalog(db, client, load_routing_config(FIXTURE))
    await client.aclose()

    rows = {m.slug: m for m in await db.scalars(select(CatalogModel))}
    assert len(rows) == len(FAKE_MODELS)
    longdoc = rows["acme/longdoc"]
    assert longdoc.context_length == 1_000_000
    assert longdoc.input_modalities == ["text", "image", "file"]
    assert "tools" in longdoc.supported_parameters
    assert longdoc.price_prompt == Decimal("0.000003")
    assert longdoc.has_zdr_endpoint is True
    assert rows["other/no-zdr"].has_zdr_endpoint is False
    assert report.warnings == []
    assert report.zdr_model_count == len(FAKE_MODELS) - 1


@respx.mock
async def test_sync_warns_about_missing_and_non_zdr_slugs(db: AsyncSession) -> None:
    models = [m for m in FAKE_MODELS if m["id"] != "acme/writer"]
    zdr = [e for e in FAKE_ZDR if e["model_id"] != "acme/thinker"]
    _mock(models, zdr)
    client = OpenRouterClient("k", BASE_URL)
    report = await sync_catalog(db, client, load_routing_config(FIXTURE))
    await client.aclose()
    assert any("acme/writer" in w and "not in the OpenRouter catalog" in w for w in report.warnings)
    assert any("acme/thinker" in w and "zero-data-retention" in w for w in report.warnings)


@respx.mock
async def test_models_unseen_for_a_day_become_unavailable(db: AsyncSession) -> None:
    _mock([catalog_model("acme/old"), *FAKE_MODELS], FAKE_ZDR)
    client = OpenRouterClient("k", BASE_URL)
    config = load_routing_config(FIXTURE)
    await sync_catalog(db, client, config)
    await db.execute(
        update(CatalogModel)
        .where(CatalogModel.slug == "acme/old")
        .values(last_seen_at=datetime.now(UTC) - timedelta(hours=25))
    )
    await db.commit()

    respx.reset()
    _mock(FAKE_MODELS, FAKE_ZDR)
    await sync_catalog(db, client, config)
    await client.aclose()
    old = await db.get(CatalogModel, "acme/old", populate_existing=True)
    assert old is not None and old.available is False
    coder = await db.get(CatalogModel, "acme/coder")
    assert coder is not None and coder.available is True


def test_shipped_routing_yaml_parses() -> None:
    config = load_routing_config(Path(__file__).resolve().parents[3] / "config" / "routing.yaml")
    assert set(config.categories) == {
        "coding",
        "data_analysis",
        "research",
        "document_qa",
        "writing",
        "reasoning",
        "actions",
        "general",
    }
