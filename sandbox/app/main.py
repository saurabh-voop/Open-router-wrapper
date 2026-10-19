"""HTTP front of the sandbox service. Only the Relay API calls it, with a shared token."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.jobs import InputFile, JobRunner, SandboxError

MAX_INPUT_BYTES = 260 * 1024 * 1024
MAX_CODE_CHARS = 200_000

app = FastAPI(title="Relay sandbox")


class FileIn(BaseModel):
    name: str = Field(max_length=300)
    content_b64: str


class JobIn(BaseModel):
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    files: list[FileIn] = Field(default_factory=list, max_length=20)


def _check_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("SANDBOX_TOKEN", "")
    given = (authorization or "").removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")


_runner: JobRunner | None = None


def runner() -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner()
    return _runner


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run", dependencies=[Depends(_check_token)])
async def run(job: JobIn) -> dict[str, Any]:
    files: list[InputFile] = []
    total = 0
    for f in job.files:
        try:
            data = base64.b64decode(f.content_b64, validate=True)
        except (binascii.Error, ValueError) as err:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "bad file encoding") from err
        total += len(data)
        if total > MAX_INPUT_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "inputs too large")
        files.append(InputFile(f.name, data))
    try:
        result = await run_in_threadpool(runner().run, job.code, files)
    except SandboxError as err:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(err)) from err
    return result.to_json()
