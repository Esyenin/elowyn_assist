"""Start the isolated personal Elowyn runtime and its pinned Hindsight service.

This launcher intentionally lives in the repository while all configuration,
secrets, logs, and persistent data remain outside it under ``~/.elowyn``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from dotenv import dotenv_values

HINDSIGHT_VERSION = "0.9.1"
DEFAULT_CONFIG = Path.home() / ".elowyn" / "personal-v0.3.env"
DEFAULT_HINDSIGHT_EXE = (
    Path.home()
    / ".elowyn"
    / "hindsight-0.9.1-venv"
    / "Scripts"
    / "hindsight-api.exe"
)
REQUIRED_CONFIG = (
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "NVIDIA_API_KEY",
    "NVIDIA_MODEL",
    "HINDSIGHT_API_URL",
    "HINDSIGHT_BANK_ID",
)
SAFE_SYSTEM_ENV = (
    "ALL_PROXY",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LOCALAPPDATA",
    "NO_PROXY",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--hindsight-exe", type=Path, default=DEFAULT_HINDSIGHT_EXE)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate isolated configuration and dependencies without starting services",
    )
    return parser.parse_args()


def _load_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"personal configuration was not found: {path}")
    values = {
        name: str(value)
        for name, value in dotenv_values(path).items()
        if value is not None
    }
    missing = [name for name in REQUIRED_CONFIG if not values.get(name, "").strip()]
    if missing:
        raise RuntimeError("personal configuration is missing: " + ", ".join(missing))
    return values


def _validate_isolation(config: dict[str, str]) -> None:
    database = urlsplit(config["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1))
    if database.path.lstrip("/") != "elowyn_personal":
        raise RuntimeError("personal launcher refuses a non-personal Core database")
    if database.username != "elowyn_personal_runtime":
        raise RuntimeError("personal launcher requires the restricted personal runtime role")

    hindsight = urlsplit(config["HINDSIGHT_API_URL"])
    if hindsight.scheme != "http" or hindsight.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("personal launcher requires a loopback Hindsight endpoint")
    if hindsight.port != 8888:
        raise RuntimeError("personal launcher requires the isolated Hindsight port 8888")

    bank_id = config["HINDSIGHT_BANK_ID"]
    if not re.fullmatch(r"elowyn-personal-[a-z0-9-]+", bank_id):
        raise RuntimeError("personal Hindsight bank must use the elowyn-personal-* namespace")
    if not config["TELEGRAM_ALLOWED_USER_ID"].isdigit():
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID must be an integer")


def _base_environment() -> dict[str, str]:
    environment = {name: os.environ[name] for name in SAFE_SYSTEM_ENV if name in os.environ}
    environment["PYTHONUTF8"] = "1"
    return environment


def _hindsight_environment(config: dict[str, str]) -> dict[str, str]:
    # Deliberately do not inherit DATABASE_URL, Telegram credentials, or any
    # ELOWYN_/TEST_ variables. Hindsight receives no Core database authority.
    environment = _base_environment()
    bank_id = config["HINDSIGHT_BANK_ID"]
    environment.update(
        {
            "HINDSIGHT_API_DATABASE_URL": f"pg0://{bank_id}",
            "HINDSIGHT_API_LLM_PROVIDER": "openai",
            "HINDSIGHT_API_LLM_API_KEY": config["NVIDIA_API_KEY"],
            "HINDSIGHT_API_LLM_MODEL": config["NVIDIA_MODEL"],
            "HINDSIGHT_API_LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
            "HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION": "false",
            "HINDSIGHT_API_WORKER_ID": f"{bank_id}-worker",
        }
    )
    return environment


def _elowyn_environment(config: dict[str, str]) -> dict[str, str]:
    environment = _base_environment()
    environment.update({name: config[name] for name in REQUIRED_CONFIG})
    return environment


def _json_endpoint(base_url: str, path: str, *, timeout: float = 3.0) -> dict[str, object]:
    with urlopen(base_url.rstrip("/") + path, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"Hindsight returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _existing_hindsight_version(base_url: str) -> str | None:
    try:
        payload = _json_endpoint(base_url, "/version")
    except (OSError, RuntimeError, URLError, ValueError):
        return None
    return str(payload.get("api_version") or "")


def _wait_ready(base_url: str, process: subprocess.Popen[bytes], timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Hindsight exited during startup; inspect the external log")
        try:
            _json_endpoint(base_url, "/health/ready")
            version = _existing_hindsight_version(base_url)
            if version != HINDSIGHT_VERSION:
                raise RuntimeError(
                    f"Hindsight API {version or 'unknown'} is not pinned {HINDSIGHT_VERSION}"
                )
            return
        except (OSError, RuntimeError, URLError, ValueError):
            time.sleep(1.0)
    raise RuntimeError("Hindsight did not become ready within five minutes")


def _stop(process: subprocess.Popen[bytes] | None, label: str) -> None:
    if process is None or process.poll() is not None:
        return
    print(f"Stopping {label}...")
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=5)


def _creation_flags() -> int:
    return subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def main() -> int:
    args = _arguments()
    project = Path(__file__).resolve().parents[1]
    project_python = project / ".venv" / "Scripts" / "python.exe"
    config_path = args.config.expanduser().resolve()
    hindsight_exe = args.hindsight_exe.expanduser().resolve()
    config = _load_config(config_path)
    _validate_isolation(config)
    if not project_python.is_file():
        raise RuntimeError(f"Elowyn environment was not found: {project_python}")
    if not hindsight_exe.is_file():
        raise RuntimeError(f"pinned Hindsight executable was not found: {hindsight_exe}")

    existing_version = _existing_hindsight_version(config["HINDSIGHT_API_URL"])
    if args.check:
        status = f"running API {existing_version}" if existing_version else "not running"
        print(f"Personal launcher check passed; Hindsight is {status}.")
        return 0
    if existing_version is not None:
        raise RuntimeError(
            "Hindsight port is already in use. Stop the separately launched personal "
            "Hindsight with Ctrl+C, then run this unified launcher again."
        )

    personal_dir = config_path.parent
    log_dir = personal_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    hindsight_log_path = log_dir / "hindsight-personal.log"
    hindsight: subprocess.Popen[bytes] | None = None
    elowyn: subprocess.Popen[bytes] | None = None
    try:
        with hindsight_log_path.open("ab", buffering=0) as hindsight_log:
            print("Starting isolated Hindsight 0.9.1...")
            hindsight = subprocess.Popen(
                [str(hindsight_exe), "--host", "127.0.0.1", "--port", "8888"],
                cwd=personal_dir,
                env=_hindsight_environment(config),
                stdout=hindsight_log,
                stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),
            )
            _wait_ready(config["HINDSIGHT_API_URL"], hindsight)
            print("Hindsight is ready. Starting Elowyn; press Ctrl+C to stop both services.")
            elowyn = subprocess.Popen(
                [str(project_python), "-m", "elowyn.app"],
                cwd=project,
                env=_elowyn_environment(config),
                creationflags=_creation_flags(),
            )
            while True:
                elowyn_status = elowyn.poll()
                hindsight_status = hindsight.poll()
                if elowyn_status is not None:
                    return elowyn_status
                if hindsight_status is not None:
                    raise RuntimeError("Hindsight stopped while Elowyn was running")
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("Shutdown requested.")
        return 0
    finally:
        _stop(elowyn, "Elowyn")
        _stop(hindsight, "Hindsight")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Launcher failed safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
