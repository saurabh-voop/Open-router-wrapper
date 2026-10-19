"""Starts one job container per request (SPEC section 8.3).

Limits: no network, 60 s wall time, 1 CPU, 1 GB RAM, ~100 MB writable disk (tmpfs),
read-only root filesystem, non-root user, no capabilities, pids limit against fork bombs.
gVisor (runsc) is used when SANDBOX_RUNTIME=runsc.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container
from docker.types import Mount

logger = logging.getLogger(__name__)

IMAGE = os.environ.get("SANDBOX_IMAGE", "relay-sandbox-runtime:1")
RUNTIME = os.environ.get("SANDBOX_RUNTIME", "")  # "runsc" for gVisor
WALL_TIME_S = float(os.environ.get("SANDBOX_WALL_TIME_S", "60"))
# The runner stops user code first so outputs can still be collected.
RUNNER_TIMEOUT_S = max(1.0, WALL_TIME_S - 5)
MEMORY = "1g"
NANO_CPUS = 1_000_000_000
PIDS_LIMIT = 128
TMPFS = {
    "/tmp": "rw,noexec,nosuid,nodev,size=32m,mode=1777",  # noqa: S108 (path inside the job)
    "/output": "rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0755",
}
MAX_LOG_BYTES = 40 * 1024 * 1024  # runner output: bounded JSON incl. base64 files
RESULT_PREFIX = "RELAY_RESULT "


@dataclass
class InputFile:
    name: str
    data: bytes


@dataclass
class JobResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    files: list[tuple[str, bytes]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "files": [
                {"name": n, "content_b64": base64.b64encode(d).decode()} for n, d in self.files
            ],
            "notes": self.notes,
            "duration_ms": self.duration_ms,
        }


class SandboxError(Exception):
    pass


def _safe_name(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lstrip(".")
    return base[:200] or "file"


def _input_tar(code: str, files: list[InputFile]) -> bytes:
    """Tar for /data: inputs read-only (root-owned 0444) plus the job script in .job/."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:

        def add(path: str, data: bytes, mode: int) -> None:
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = mode
            info.uid = info.gid = 0
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))

        job_dir = tarfile.TarInfo(".job")
        job_dir.type = tarfile.DIRTYPE
        job_dir.mode = 0o755
        tar.addfile(job_dir)
        add(".job/main.py", code.encode(), 0o444)
        seen: set[str] = set()
        for f in files:
            name = _safe_name(f.name)
            while name in seen:
                name = "_" + name
            seen.add(name)
            add(name, f.data, 0o444)
    return buf.getvalue()


def _parse_result(logs: bytes, timed_out: bool) -> JobResult:
    for line in reversed(logs.decode("utf-8", errors="replace").splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                raw = json.loads(line[len(RESULT_PREFIX) :])
                return JobResult(
                    exit_code=int(raw.get("exit_code", -1)),
                    timed_out=bool(raw.get("timed_out")) or timed_out,
                    stdout=str(raw.get("stdout", "")),
                    stderr=str(raw.get("stderr", "")),
                    files=[
                        (str(f["name"]), base64.b64decode(f["b64"])) for f in raw.get("files", [])
                    ],
                    notes=[str(n) for n in raw.get("notes", [])],
                )
            except (ValueError, KeyError, TypeError):
                break
    if timed_out:
        return JobResult(-9, True, "", "[killed: time limit reached]")
    return JobResult(
        -1, False, "", "[the job ended without a result; it may have run out of memory]"
    )


class JobRunner:
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self.client = client or docker.from_env()

    def run(self, code: str, files: list[InputFile]) -> JobResult:
        started = time.monotonic()
        name = f"relay-job-{uuid.uuid4().hex[:12]}"
        kwargs: dict[str, Any] = {
            "image": IMAGE,
            "name": name,
            "detach": True,
            "network_mode": "none",
            "read_only": True,
            "user": "10001:10001",
            "mem_limit": MEMORY,
            "memswap_limit": MEMORY,
            "nano_cpus": NANO_CPUS,
            "pids_limit": PIDS_LIMIT,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "tmpfs": TMPFS,
            "mounts": [Mount(target="/data", source=None, type="volume")],
            "environment": {"RELAY_TIMEOUT_S": str(RUNNER_TIMEOUT_S)},
            "labels": {"relay.sandbox": "job"},
            "log_config": {"type": "json-file", "config": {"max-size": "50m"}},
        }
        if RUNTIME:
            kwargs["runtime"] = RUNTIME
        container: Container | None = None
        try:
            container = self.client.containers.create(**kwargs)
            if not container.put_archive("/data", _input_tar(code, files)):
                raise SandboxError("could not copy inputs into the job container")
            container.start()
            timed_out = False
            try:
                container.wait(timeout=WALL_TIME_S)
            except Exception:  # requests' ReadTimeout or ConnectionError on timeout
                timed_out = True
                container.kill()
                container.wait(timeout=10)
            logs = container.logs(stdout=True, stderr=False)[-MAX_LOG_BYTES:]
            result = _parse_result(logs, timed_out)
        except (APIError, DockerException) as err:
            logger.warning("sandbox docker error: %s", type(err).__name__)
            raise SandboxError("the sandbox could not run the job") from err
        finally:
            if container is not None:
                try:
                    container.remove(v=True, force=True)
                except NotFound:
                    pass
                except (APIError, DockerException):
                    logger.warning("could not remove job container %s", name)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result
