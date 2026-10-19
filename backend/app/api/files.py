"""File uploads and downloads (SPEC sections 8.2 and 12). Users only reach their own files."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import select

import app.services.files as file_service
from app.api.conversations import get_owned_conversation
from app.api.deps import CurrentUser, DbSession
from app.models import Attachment
from app.schemas.chat import AttachmentOut
from app.services import attachments as att_service
from app.services import virus_scan

router = APIRouter(prefix="/files", tags=["files"])

INLINE_MIMES = att_service.IMAGE_MIMES | att_service.PDF_MIMES


@router.post("", response_model=AttachmentOut, status_code=status.HTTP_201_CREATED)
async def upload(
    user: CurrentUser,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    conversation_id: Annotated[uuid.UUID | None, Form()] = None,
) -> Attachment:
    if conversation_id is not None:
        await get_owned_conversation(db, user.id, conversation_id)
    data = await file.read(att_service.MAX_FILE_BYTES + 1)
    if len(data) > att_service.MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Files can be at most 25 MB")
    filename = att_service.safe_filename(file.filename)
    try:
        # PDF/Excel parsing is CPU-bound: keep it off the event loop so streams stay live.
        info = await asyncio.to_thread(att_service.inspect, data, filename, file.content_type or "")
    except att_service.AttachmentError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(err)) from err
    try:
        await virus_scan.scan(data)
    except virus_scan.VirusFound as err:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{filename}: rejected by the virus scanner"
        ) from err
    except virus_scan.ScannerUnavailable as err:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The virus scanner is unavailable; try again."
        ) from err

    attachment_id = uuid.uuid4()
    key = f"files/{user.id}/{attachment_id}"
    await file_service.put_object(key, data, info.mime)
    att = Attachment(
        id=attachment_id,
        user_id=user.id,
        conversation_id=conversation_id,
        s3_key=key,
        filename=filename,
        mime=info.mime,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        page_count=info.page_count,
        est_tokens=info.est_tokens,
    )
    db.add(att)
    await db.commit()
    return att


@router.get("/{attachment_id}")
async def download(attachment_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    att = await db.scalar(
        select(Attachment).where(Attachment.id == attachment_id, Attachment.user_id == user.id)
    )
    if att is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    data = await file_service.get_object(att.s3_key)
    disposition = "inline" if att.mime in INLINE_MIMES else "attachment"
    ascii_name = att.filename.encode("ascii", "ignore").decode().replace('"', "") or "file"
    return Response(
        content=data,
        media_type=att.mime,
        headers={
            "Content-Disposition": (
                f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(att.filename)}"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )
