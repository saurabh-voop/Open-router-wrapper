"""The ``run_python`` function tool (SPEC section 8.3).

Code runs in the local sandbox service, so client data never leaves the server for this
step. The conversation's files are mounted read-only at /data; files written to /output
are stored in S3 and shown in the chat.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.files as file_service
from app.agent.tools.base import OutputFile, ToolContext, ToolResult
from app.models import Attachment, Conversation, User

logger = logging.getLogger(__name__)

MAX_INPUT_BYTES = 250 * 1024 * 1024
MAX_RESULT_CHARS = 20 * 1024  # what the model sees from stdout/stderr (SPEC 8.3)
SANDBOX_TIMEOUT_S = 90  # the job itself is capped at 60 s inside the sandbox
OUTPUT_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".json": "text/plain",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class SandboxClient:
    def __init__(self, url: str, token: str, http: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.http = http or httpx.AsyncClient(timeout=SANDBOX_TIMEOUT_S)

    async def run(self, code: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
        resp = await self.http.post(
            f"{self.url}/run",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "code": code,
                "files": [
                    {"name": n, "content_b64": base64.b64encode(d).decode()} for n, d in files
                ],
            },
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n[... truncated, {len(text)} characters total]"


def _output_mime(name: str) -> str:
    dot = name.rfind(".")
    ext = name[dot:].lower() if dot >= 0 else ""
    return OUTPUT_MIMES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


class RunPythonTool:
    name = "run_python"
    writes = False

    def __init__(
        self,
        client: SandboxClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        user: User,
        conversation: Conversation,
        filenames: list[str],
    ) -> None:
        self.client = client
        self.sessionmaker = sessionmaker
        self.user = user
        self.conversation = conversation
        self.filenames = filenames

    def spec(self) -> dict[str, Any]:
        files = ", ".join(self.filenames) if self.filenames else "none"
        return {
            "type": "function",
            "function": {
                "name": "run_python",
                "description": (
                    "Run Python 3.12 code in an isolated sandbox with pandas, numpy, matplotlib "
                    "and openpyxl. No network access. 60 second limit. The conversation's files "
                    f"are read-only in /data (available: {files}). Save charts and files to "
                    "/output; they are shown to the user. Print results to stdout."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string", "description": "Python source"}},
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return ToolResult("Error: `code` must be a non-empty string.", "no code", ok=False)
        async with self.sessionmaker() as db:
            inputs = await self._inputs(db)
            try:
                result = await self.client.run(code, inputs)
            except httpx.HTTPError as err:
                logger.warning("sandbox call failed: %s", type(err).__name__)
                return ToolResult(
                    "The code sandbox is unavailable right now.", "sandbox unavailable", ok=False
                )
            files = await self._store_outputs(db, result.get("files") or [])
            await db.commit()

        exit_code = result.get("exit_code")
        lines = [
            f"exit_code: {exit_code}" + (" (killed: time limit)" if result.get("timed_out") else "")
        ]
        if result.get("stdout"):
            lines.append("stdout:\n" + _clip(str(result["stdout"])))
        if result.get("stderr"):
            lines.append("stderr:\n" + _clip(str(result["stderr"])))
        if files:
            lines.append(
                "output files (already shown to the user): " + ", ".join(f.filename for f in files)
            )
        for note in result.get("notes") or []:
            lines.append(f"note: {note}")
        ok = exit_code == 0 and not result.get("timed_out")
        summary = f"exit {exit_code}, {len(files)} file(s)" + (
            ", timed out" if result.get("timed_out") else ""
        )
        return ToolResult("\n".join(lines), summary, ok=ok, files=files)

    async def _inputs(self, db: AsyncSession) -> list[tuple[str, bytes]]:
        rows = await db.scalars(
            select(Attachment)
            .where(
                Attachment.conversation_id == self.conversation.id,
                Attachment.user_id == self.user.id,
            )
            .order_by(Attachment.created_at)
        )
        files: list[tuple[str, bytes]] = []
        total = 0
        for att in rows:
            if total + att.size_bytes > MAX_INPUT_BYTES:
                break
            files.append((att.filename, await file_service.get_object(att.s3_key)))
            total += att.size_bytes
        return files

    async def _store_outputs(
        self, db: AsyncSession, raw_files: list[dict[str, Any]]
    ) -> list[OutputFile]:
        out: list[OutputFile] = []
        for f in raw_files:
            name = str(f.get("name") or "output").replace("/", "_")[:200]
            data = base64.b64decode(str(f.get("content_b64") or ""))
            mime = _output_mime(name)
            att_id = uuid.uuid4()
            key = f"files/{self.user.id}/{att_id}"
            await file_service.put_object(key, data, mime)
            db.add(
                Attachment(
                    id=att_id,
                    user_id=self.user.id,
                    conversation_id=self.conversation.id,
                    s3_key=key,
                    filename=name,
                    mime=mime,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    est_tokens=None,
                )
            )
            out.append(OutputFile(attachment_id=att_id, filename=name, mime=mime))
        return out


def register(
    provider: Any, client: SandboxClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async def factory(user: User, conversation: Conversation) -> list[Any]:
        async with sessionmaker() as db:
            names = list(
                await db.scalars(
                    select(Attachment.filename).where(
                        Attachment.conversation_id == conversation.id,
                        Attachment.user_id == user.id,
                    )
                )
            )
        return [RunPythonTool(client, sessionmaker, user, conversation, names)]

    provider.register("run_python", factory)
