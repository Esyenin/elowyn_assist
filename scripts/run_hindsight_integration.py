"""Run Elowyn's mandatory integration gate against real Hindsight 0.9.1.

The backend is an ephemeral official container using Hindsight's built-in mock
LLM and embedded pg0 database. No Core database or external credentials are
provided to the container.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

HINDSIGHT_IMAGE = "ghcr.io/vectorize-io/hindsight:0.9.1"
READINESS_TIMEOUT_SECONDS = 300
INTEGRATION_TESTS = (
    "tests/integration/test_hindsight_adapter.py",
    "tests/integration/test_memory_v02_acceptance.py",
)


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _mapped_port(container_name: str) -> int:
    output = _run(["docker", "port", container_name, "8888/tcp"]).stdout
    match = re.search(r":(\d+)\s*$", output.splitlines()[0]) if output else None
    if match is None:
        raise RuntimeError("Docker did not publish the Hindsight API port")
    return int(match.group(1))


def _wait_ready(base_url: str, container_name: str) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health/ready", timeout=2) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = type(exc).__name__
        status = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip().casefold() != "true":
            raise RuntimeError("Hindsight container stopped before readiness")
        time.sleep(1)
    raise TimeoutError(f"Hindsight readiness timed out ({last_error})")


def _show_logs(container_name: str) -> None:
    logs = _run(["docker", "logs", "--tail", "200", container_name], check=False)
    if logs.stdout:
        print(logs.stdout, file=sys.stderr)


def _github_error(message: str) -> None:
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title=Real Hindsight integration gate::{escaped}")


def main() -> int:
    if shutil.which("docker") is None:
        print("Docker is required for the real Hindsight integration gate.", file=sys.stderr)
        return 2

    container_name = f"elowyn-hindsight-integration-{uuid.uuid4().hex[:12]}"
    started = False
    try:
        result = _run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--publish",
                "127.0.0.1::8888",
                "--env",
                "HINDSIGHT_API_DATABASE_URL=pg0",
                "--env",
                "HINDSIGHT_API_LLM_PROVIDER=mock",
                "--env",
                "HINDSIGHT_API_LLM_API_KEY=synthetic-test-only",
                "--env",
                "HINDSIGHT_API_LLM_MODEL=mock-model",
                "--env",
                "HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=false",
                HINDSIGHT_IMAGE,
            ],
            check=False,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            return result.returncode
        started = True
        base_url = f"http://127.0.0.1:{_mapped_port(container_name)}"
        _wait_ready(base_url, container_name)

        test_environment = os.environ.copy()
        test_environment["ELOWYN_TEST_HINDSIGHT_URL"] = base_url
        test_environment["ELOWYN_TEST_HINDSIGHT_CONTAINER"] = container_name
        test_environment["ELOWYN_FAIL_ON_SKIP"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *INTEGRATION_TESTS,
                "-m",
                "hindsight",
                "-q",
                "-ra",
                "--tb=short",
            ],
            env=test_environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if completed.stdout:
            print(completed.stdout)
        if completed.returncode != 0:
            _github_error((completed.stdout or "pytest failed")[-8000:])
            _show_logs(container_name)
        return completed.returncode
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"Hindsight integration harness failed: {exc}", file=sys.stderr)
        _github_error(str(exc))
        if started:
            _show_logs(container_name)
        return 1
    finally:
        if started:
            _run(["docker", "rm", "--force", container_name], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
