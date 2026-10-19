"""Sandbox security tests (TASKS Phase 6). They run real job containers.

Needs a Docker daemon and the runtime image:
    docker build -t relay-sandbox-runtime:1 sandbox/runtime
Skipped when either is missing. Set SANDBOX_WALL_TIME_S=10 to keep the timeout test short.
"""

import pytest

from app.jobs import IMAGE, InputFile, JobRunner

docker = pytest.importorskip("docker")


def _runner() -> JobRunner:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
    except Exception as err:  # daemon down or image not built
        pytest.skip(f"sandbox integration unavailable: {type(err).__name__}")
    return JobRunner(client)


@pytest.fixture(scope="module")
def runner() -> JobRunner:
    return _runner()


def test_runs_code_reads_data_and_returns_outputs(runner: JobRunner) -> None:
    code = (
        "import pandas as pd, matplotlib.pyplot as plt\n"
        "df = pd.read_csv('/data/sales.csv')\n"
        "print(df['amount'].sum())\n"
        "df.plot(x='month', y='amount'); plt.savefig('/output/chart.png')\n"
    )
    result = runner.run(code, [InputFile("sales.csv", b"month,amount\nJan,10\nFeb,32\n")])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "42"
    names = [n for n, _ in result.files]
    assert names == ["chart.png"]
    assert result.files[0][1].startswith(b"\x89PNG")


def test_network_access_fails(runner: JobRunner) -> None:
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('BLOCKED', type(e).__name__)\n"
        "try:\n"
        "    socket.getaddrinfo('pypi.org', 443)\n"
        "    print('DNS')\n"
        "except OSError:\n"
        "    print('NODNS')\n"
    )
    result = runner.run(code, [])
    assert "CONNECTED" not in result.stdout and "BLOCKED" in result.stdout
    assert "NODNS" in result.stdout


def test_writing_outside_output_fails(runner: JobRunner) -> None:
    code = (
        "import os\n"
        "for path in ['/data/sales.csv', '/data/new.txt', '/etc/passwd', '/usr/x', '/x']:\n"
        "    try:\n"
        "        open(path, 'a').write('x')\n"
        "        print('WROTE', path)\n"
        "    except OSError as e:\n"
        "        print('DENIED', path)\n"
        "open('/output/ok.txt', 'w').write('fine')\n"
        "print('OUTPUT_OK')\n"
    )
    result = runner.run(code, [InputFile("sales.csv", b"a,b\n")])
    assert "WROTE" not in result.stdout, result.stdout
    assert result.stdout.count("DENIED") == 5
    assert "OUTPUT_OK" in result.stdout


def test_infinite_loop_is_killed(runner: JobRunner) -> None:
    result = runner.run("while True:\n    pass\n", [])
    assert result.timed_out is True


def test_fork_bomb_is_contained(runner: JobRunner) -> None:
    code = (
        "import os\n"
        "children = 0\n"
        "try:\n"
        "    while True:\n"
        "        if os.fork() == 0:\n"
        "            import time; time.sleep(30); os._exit(0)\n"
        "        children += 1\n"
        "except OSError:\n"
        "    print('LIMIT', children)\n"
    )
    result = runner.run(code, [])
    assert "LIMIT" in result.stdout, (result.stdout, result.stderr)
    assert int(result.stdout.split()[1]) < 128
    # The daemon is still healthy and runs the next job.
    assert runner.run("print('alive')", []).stdout.strip() == "alive"


def test_memory_limit(runner: JobRunner) -> None:
    result = runner.run("x = bytearray(3 * 1024**3)\nprint('ALLOCATED')\n", [])
    assert "ALLOCATED" not in result.stdout


def test_runs_as_non_root_without_capabilities(runner: JobRunner) -> None:
    code = (
        "import os\n"
        "print(os.getuid(), os.getgid())\n"
        "print(open('/proc/self/status').read().split('CapEff:')[1].split()[0])\n"
    )
    result = runner.run(code, [])
    uid_line, cap_line = result.stdout.split("\n")[:2]
    assert uid_line == "10001 10001"
    assert int(cap_line, 16) == 0
