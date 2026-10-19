"""Optional ClamAV scan over the clamd INSTREAM protocol (SPEC section 8.2: virus check).

Enabled when CLAMAV_HOST is set. Without it uploads are only type-checked; the admin
catalog page shows a warning so this is a visible choice, not a silent gap.
"""

from __future__ import annotations

import asyncio
import struct

from app.core.config import get_settings

CHUNK = 64 * 1024


class VirusFound(Exception):
    pass


class ScannerUnavailable(Exception):
    pass


async def scan(data: bytes) -> None:
    settings = get_settings()
    if not settings.clamav_host:
        return
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.clamav_host, settings.clamav_port), timeout=5
        )
    except (OSError, TimeoutError) as err:
        raise ScannerUnavailable("virus scanner unreachable") from err
    try:
        writer.write(b"zINSTREAM\0")
        for start in range(0, len(data), CHUNK):
            chunk = data[start : start + CHUNK]
            writer.write(struct.pack(">I", len(chunk)) + chunk)
        writer.write(struct.pack(">I", 0))
        await writer.drain()
        reply = (await asyncio.wait_for(reader.read(4096), timeout=60)).decode(errors="replace")
    finally:
        writer.close()
    if "FOUND" in reply:
        raise VirusFound(reply.strip("\0 \n"))
    if "OK" not in reply:
        raise ScannerUnavailable(f"unexpected scanner reply: {reply[:100]}")
