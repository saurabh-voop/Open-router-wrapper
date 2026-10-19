"""Model catalog sync (SPEC section 7).

Runs on startup and hourly: pulls the account-filtered model list and the ZDR endpoint
list from OpenRouter, upserts the ``models`` table, marks models unseen for 24 hours as
unavailable, and reports routing.yaml slugs that are missing or have no ZDR endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.openrouter import OpenRouterClient, OpenRouterError
from app.models import CatalogModel
from app.router.config import RoutingConfig

logger = logging.getLogger(__name__)

UNAVAILABLE_AFTER = timedelta(hours=24)
SYNC_INTERVAL_S = 3600


@dataclass
class CatalogReport:
    synced_at: datetime | None = None
    model_count: int = 0
    zdr_model_count: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


# Last sync result, shown on the admin page.
last_report = CatalogReport()


def _price(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _context_length(model: dict[str, Any]) -> int:
    value = model.get("context_length") or (model.get("top_provider") or {}).get("context_length")
    return int(value or 0)


def model_rows(
    models: list[dict[str, Any]], zdr_endpoints: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    zdr_ids = {e.get("model_id") for e in zdr_endpoints if e.get("model_id")}
    rows = []
    for m in models:
        slug = m.get("id")
        if not slug:
            continue
        arch = m.get("architecture") or {}
        pricing = m.get("pricing") or {}
        rows.append(
            {
                "slug": slug,
                "name": m.get("name") or slug,
                "context_length": _context_length(m),
                "input_modalities": list(arch.get("input_modalities") or []),
                "supported_parameters": list(m.get("supported_parameters") or []),
                "price_prompt": _price(pricing.get("prompt")),
                "price_completion": _price(pricing.get("completion")),
                "has_zdr_endpoint": slug in zdr_ids or m.get("canonical_slug") in zdr_ids,
                "available": True,
                "last_seen_at": now,
            }
        )
    return rows


def routing_warnings(config: RoutingConfig, catalog: dict[str, CatalogModel]) -> list[str]:
    warnings = [f"routing.yaml still has placeholders in: {p}" for p in config.placeholders()]
    for slug in sorted(config.all_slugs()):
        row = catalog.get(slug)
        if row is None or not row.available:
            warnings.append(f"{slug}: not in the OpenRouter catalog for this account")
        elif not row.has_zdr_endpoint:
            warnings.append(f"{slug}: has no zero-data-retention endpoint")
    return warnings


async def load_catalog(db: AsyncSession) -> dict[str, CatalogModel]:
    return {m.slug: m for m in await db.scalars(select(CatalogModel))}


async def sync_catalog(
    db: AsyncSession, client: OpenRouterClient, config: RoutingConfig
) -> CatalogReport:
    now = datetime.now(UTC)
    models = await client.list_user_models()
    zdr = await client.list_zdr_endpoints()
    rows = model_rows(models, zdr, now)

    if rows:
        stmt = insert(CatalogModel).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[CatalogModel.slug],
            set_={
                c: stmt.excluded[c]
                for c in (
                    "name",
                    "context_length",
                    "input_modalities",
                    "supported_parameters",
                    "price_prompt",
                    "price_completion",
                    "has_zdr_endpoint",
                    "available",
                    "last_seen_at",
                )
            },
        )
        await db.execute(stmt)
    await db.execute(
        update(CatalogModel)
        .where(CatalogModel.last_seen_at < now - UNAVAILABLE_AFTER)
        .values(available=False)
    )
    await db.commit()

    report = CatalogReport(
        synced_at=now,
        model_count=len(rows),
        zdr_model_count=sum(1 for r in rows if r["has_zdr_endpoint"]),
        warnings=routing_warnings(config, await load_catalog(db)),
    )
    for w in report.warnings:
        logger.warning("catalog: %s", w)
    return report


async def run_sync_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: OpenRouterClient,
    config: RoutingConfig,
) -> CatalogReport:
    global last_report
    try:
        async with sessionmaker() as db:
            last_report = await sync_catalog(db, client, config)
    except (OpenRouterError, OSError) as err:
        logger.warning("catalog sync failed: %s", err)
        last_report = CatalogReport(
            synced_at=last_report.synced_at,
            model_count=last_report.model_count,
            zdr_model_count=last_report.zdr_model_count,
            warnings=last_report.warnings,
            error=str(err),
        )
    return last_report


async def sync_forever(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: OpenRouterClient,
    config: RoutingConfig,
    interval_s: float = SYNC_INTERVAL_S,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await run_sync_once(sessionmaker, client, config)
