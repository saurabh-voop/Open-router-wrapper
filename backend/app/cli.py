"""Admin CLI. Usage:

    uv run python -m app.cli create-admin --email you@example.com --name "Your Name"

The password is read from the RELAY_ADMIN_PASSWORD env var or prompted for.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from decimal import Decimal

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_engine, get_sessionmaker
from app.core.security import hash_password
from app.models import User

MIN_PASSWORD_LEN = 12


async def create_user(email: str, name: str, password: str, role: str) -> None:
    async with get_sessionmaker()() as db:
        existing = await db.scalar(select(User).where(func.lower(User.email) == email.lower()))
        if existing is not None:
            raise SystemExit(f"A user with email {email} already exists.")
        db.add(
            User(
                email=email.lower(),
                name=name,
                password_hash=hash_password(password),
                role=role,
                monthly_cap_usd=Decimal(str(get_settings().default_monthly_cap_usd)),
            )
        )
        await db.commit()
    await get_engine().dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="relay")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin", help="Create an admin account")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="")
    args = parser.parse_args(argv)

    password = os.environ.get("RELAY_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LEN:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    asyncio.run(create_user(args.email, args.name, password, "admin"))
    print(f"Created admin {args.email}")


if __name__ == "__main__":
    main()
