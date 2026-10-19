"""Runs inside the job container. Executes /data/.job/main.py and prints one result line.

The result is a single line ``RELAY_RESULT <json>`` on stdout. Everything the user code
prints is captured through pipes and truncated, so the container's own stdout stays small.
"""

import base64
import json
import os
import subprocess
import sys

CODE_PATH = "/data/.job/main.py"
OUTPUT_DIR = "/output"
TIMEOUT_S = float(os.environ.get("RELAY_TIMEOUT_S", "55"))
MAX_STREAM_BYTES = 20 * 1024
MAX_OUTPUT_FILES = 20
MAX_OUTPUT_BYTES = 20 * 1024 * 1024


def _clip(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(data) > MAX_STREAM_BYTES:
        return text[:MAX_STREAM_BYTES] + f"\n[... truncated, {len(data)} bytes total]"
    return text


def _collect_outputs() -> tuple[list[dict[str, str]], list[str]]:
    files: list[dict[str, str]] = []
    notes: list[str] = []
    total = 0
    for root, _dirs, names in os.walk(OUTPUT_DIR):
        for name in sorted(names):
            path = os.path.join(root, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, OUTPUT_DIR)
            size = os.path.getsize(path)
            if len(files) >= MAX_OUTPUT_FILES or total + size > MAX_OUTPUT_BYTES:
                notes.append(f"skipped {rel}: output limit reached")
                continue
            with open(path, "rb") as fh:
                files.append({"name": rel, "b64": base64.b64encode(fh.read()).decode()})
            total += size
    return files, notes


def main() -> None:
    timed_out = False
    try:
        proc = subprocess.run(  # noqa: S603 (running the job is the point)
            [sys.executable, "-I", CODE_PATH],
            cwd=OUTPUT_DIR,
            capture_output=True,
            timeout=TIMEOUT_S,
            check=False,
        )
        exit_code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -9
        out, err = exc.stdout or b"", (exc.stderr or b"") + b"\n[killed: time limit reached]"
    files, notes = _collect_outputs()
    result = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": _clip(out),
        "stderr": _clip(err),
        "files": files,
        "notes": notes,
    }
    sys.stdout.write("RELAY_RESULT " + json.dumps(result) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
