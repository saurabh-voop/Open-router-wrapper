"""Strip NUL characters before rows reach Postgres.

Postgres text and jsonb reject "\\u0000". Model output, tool results (e.g. a script that
prints a NUL) and pasted text can contain it, and the resulting DB error would fail the
turn. This runs on every flush, so no write path can forget it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, UOWTransaction


def strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    return value


@event.listens_for(Session, "before_flush")
def _strip_nul_before_flush(session: Session, _ctx: UOWTransaction, _instances: Any) -> None:
    for obj in (*session.new, *session.dirty):
        for attr in inspect(obj).mapper.column_attrs:
            value = getattr(obj, attr.key, None)
            if isinstance(value, (str, list, dict)):
                cleaned = strip_nul(value)
                if cleaned is not value and cleaned != value:
                    setattr(obj, attr.key, cleaned)
