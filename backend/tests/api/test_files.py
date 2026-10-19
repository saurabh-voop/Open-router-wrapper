"""Uploads, attachment routing and file isolation (SPEC 8.2, TASKS Phase 5)."""

import io
import uuid
from collections.abc import Iterator

import pytest
import respx
from httpx import AsyncClient
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, Conversation, User
from app.services.files import s3_client
from tests.api.test_chat import FakeOpenRouter, new_conversation, send
from tests.fakes import CHAT_URL, text_stream
from tests.helpers import login, make_user, seed_catalog

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def fake() -> Iterator[FakeOpenRouter]:
    f = FakeOpenRouter()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(CHAT_URL).mock(side_effect=f)
        yield f


@pytest.fixture
async def alice(client: AsyncClient, db: AsyncSession) -> User:
    await seed_catalog(db)
    user = await make_user(db, "alice@paikane.com")
    await login(client, "alice@paikane.com")
    return user


async def upload(
    client: AsyncClient, name: str, data: bytes, mime: str, conv_id: str | None = None
) -> dict[str, object]:
    form = {"conversation_id": conv_id} if conv_id else None
    resp = await client.post("/files", files={"file": (name, data, mime)}, data=form)
    assert resp.status_code == 201, resp.text
    body: dict[str, object] = resp.json()
    return body


def bucket_keys() -> list[str]:
    listing = s3_client().list_objects_v2(Bucket="relay-test")
    return [o["Key"] for o in listing.get("Contents", [])]


async def test_upload_stores_object_and_row(
    client: AsyncClient, db: AsyncSession, alice: User
) -> None:
    att = await upload(client, "../../etc/tender.pdf", pdf_bytes(3), "application/pdf")
    assert att["filename"] == "tender.pdf"
    assert att["mime"] == "application/pdf" and att["page_count"] == 3
    row = await db.get(Attachment, uuid.UUID(str(att["id"])))
    assert row is not None and row.s3_key == f"files/{alice.id}/{row.id}"
    assert row.s3_key in bucket_keys()
    got = await client.get(f"/files/{att['id']}")
    assert got.status_code == 200 and got.content.startswith(b"%PDF-")
    assert got.headers["x-content-type-options"] == "nosniff"


async def test_type_is_sniffed_not_trusted(client: AsyncClient, alice: User) -> None:
    resp = await client.post(
        "/files", files={"file": ("evil.pdf", b"MZ\x90\x00binary", "application/pdf")}
    )
    assert resp.status_code == 422


async def test_size_limit(client: AsyncClient, alice: User) -> None:
    big = b"a" * (25 * 1024 * 1024 + 1)
    resp = await client.post("/files", files={"file": ("big.txt", big, "text/plain")})
    assert resp.status_code == 413


async def test_fifty_page_pdf_routes_to_long_context_file_model(
    client: AsyncClient, alice: User, fake: FakeOpenRouter
) -> None:
    fake.category = "document_qa"
    fake.streams = [text_stream("Payment is due in 30 days.", model="acme/longdoc")]
    conv_id = await new_conversation(client)
    att = await upload(client, "tender.pdf", pdf_bytes(50), "application/pdf", conv_id)
    assert att["page_count"] == 50
    events = await send(client, conv_id, "What are the payment terms?", attachment_ids=[att["id"]])
    assert events[0]["model"] == "acme/longdoc"
    assert events[0]["confidential"] is True  # default on once a chat has an attachment
    content = fake.chat_bodies[0]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "What are the payment terms?"}
    assert content[1]["type"] == "file"
    assert content[1]["file"]["filename"] == "tender.pdf"
    assert content[1]["file"]["file_data"].startswith("data:application/pdf;base64,")


async def test_image_is_sent_as_image_url_and_needs_vision(
    client: AsyncClient, alice: User, fake: FakeOpenRouter
) -> None:
    fake.category = "writing"
    conv_id = await new_conversation(client)
    att = await upload(client, "site.png", PNG, "image/png", conv_id)
    events = await send(client, conv_id, "Describe this site photo", attachment_ids=[att["id"]])
    assert events[0]["model"] == "acme/allround"  # writer has no vision
    part = fake.chat_bodies[0]["messages"][-1]["content"][1]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


async def test_text_file_is_inlined(client: AsyncClient, alice: User, fake: FakeOpenRouter) -> None:
    conv_id = await new_conversation(client)
    att = await upload(client, "notes.md", b"# Notes\nGenset 500 kVA", "text/markdown", conv_id)
    await send(client, conv_id, "Summarise my notes", attachment_ids=[att["id"]])
    part = fake.chat_bodies[0]["messages"][-1]["content"][1]
    assert part["type"] == "text" and "Genset 500 kVA" in part["text"]


async def test_delete_conversation_deletes_s3_objects(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    conv_id = await new_conversation(client)
    att = await upload(client, "a.pdf", pdf_bytes(1), "application/pdf", conv_id)
    await send(client, conv_id, "read it", attachment_ids=[att["id"]])
    key = f"files/{alice.id}/{att['id']}"
    assert key in bucket_keys()
    assert (await client.delete(f"/conversations/{conv_id}")).status_code == 204
    assert key not in bucket_keys()
    assert (
        await db.scalar(select(Attachment).where(Attachment.id == uuid.UUID(str(att["id"]))))
        is None
    )
    assert await db.get(Conversation, uuid.UUID(conv_id)) is None


async def test_attachment_cannot_be_reused_by_another_user(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    att = await upload(client, "a.pdf", pdf_bytes(1), "application/pdf")
    alice_conv = await new_conversation(client)
    await make_user(db, "bob@paikane.com")
    await login(client, "bob@paikane.com")
    assert (await client.get(f"/files/{att['id']}")).status_code == 404
    bob_conv = await new_conversation(client)
    resp = await client.post(
        f"/conversations/{bob_conv}/messages", json={"text": "x", "attachment_ids": [att["id"]]}
    )
    assert resp.status_code == 404
    resp = await client.post(
        "/files",
        files={"file": ("b.txt", b"hello", "text/plain")},
        data={"conversation_id": alice_conv},
    )
    assert resp.status_code == 404
