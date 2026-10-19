import base64
import io
import json
import tarfile

from fastapi.testclient import TestClient

from app import main
from app.jobs import InputFile, JobResult, _input_tar, _parse_result


def test_input_tar_is_root_owned_and_read_only() -> None:
    data = _input_tar(
        "print(1)", [InputFile("../../sales.csv", b"a,b"), InputFile("sales.csv", b"c")]
    )
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert set(members) == {".job", ".job/main.py", "sales.csv", "_sales.csv"}
    for m in members.values():
        assert m.uid == 0 and m.gid == 0
        if m.isfile():
            assert m.mode == 0o444


def test_parse_result_reads_last_marker_line() -> None:
    payload = {"exit_code": 0, "timed_out": False, "stdout": "hi", "stderr": "", "files": [
        {"name": "chart.png", "b64": base64.b64encode(b"PNG").decode()}], "notes": []}  # fmt: skip
    logs = b"noise\nRELAY_RESULT " + json.dumps(payload).encode() + b"\n"
    result = _parse_result(logs, timed_out=False)
    assert result.stdout == "hi" and result.files == [("chart.png", b"PNG")]


def test_parse_result_without_marker() -> None:
    assert _parse_result(b"garbage", timed_out=True).timed_out is True
    assert _parse_result(b"garbage", timed_out=False).exit_code == -1


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[InputFile]]] = []

    def run(self, code: str, files: list[InputFile]) -> JobResult:
        self.calls.append((code, files))
        return JobResult(0, False, "ok", "", files=[("out.txt", b"x")])


def test_api_requires_token(monkeypatch: object) -> None:
    import pytest

    mp = pytest.MonkeyPatch()
    mp.setenv("SANDBOX_TOKEN", "s3cret-token")
    fake = FakeRunner()
    mp.setattr(main, "_runner", fake)
    client = TestClient(main.app)
    body = {"code": "print(1)", "files": [{"name": "a.csv", "content_b64": "YSxi"}]}
    assert client.post("/run", json=body).status_code == 401
    assert (
        client.post("/run", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    resp = client.post("/run", json=body, headers={"Authorization": "Bearer s3cret-token"})
    assert resp.status_code == 200
    assert resp.json()["files"][0] == {"name": "out.txt", "content_b64": "eA=="}
    assert fake.calls[0][1][0].data == b"a,b"
    mp.undo()


def test_api_refuses_when_no_token_configured() -> None:
    import pytest

    mp = pytest.MonkeyPatch()
    mp.delenv("SANDBOX_TOKEN", raising=False)
    client = TestClient(main.app)
    resp = client.post("/run", json={"code": "x"}, headers={"Authorization": "Bearer "})
    assert resp.status_code == 401
    mp.undo()
