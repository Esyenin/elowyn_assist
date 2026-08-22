"""Scan the worktree and every Git commit without printing candidate secret values."""

from __future__ import annotations

import io
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    "provider/API token": re.compile(
        rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})"
    ),
    "Telegram token": re.compile(rb"\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}\b"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credentialed PostgreSQL DSN": re.compile(
        rb"postgres(?:ql)?(?:\+asyncpg)?://([^\s:/@]+):([^\s@]+)@([^\s/]+)/(\S+)"
    ),
}
PLACEHOLDER_PASSWORDS = {b"elowyn", b"postgres"}


def command(*args: str) -> str:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def allowed_placeholder(label: str, match: re.Match[bytes]) -> bool:
    if label != "credentialed PostgreSQL DSN":
        return False
    password = match.group(2)
    host = match.group(3).split(b":", 1)[0]
    return password in PLACEHOLDER_PASSWORDS and host in {b"localhost", b"127.0.0.1"}


def inspect(content: bytes, location: str) -> list[str]:
    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(content):
            if not allowed_placeholder(label, match):
                findings.append(f"{label}: {location}")
    return findings


def inspect_file(content: bytes, path: str, location: str) -> list[str]:
    findings = inspect(content, location)
    if path.lower().endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for member in archive.namelist():
                    if member.endswith((".xml", ".rels")):
                        findings.extend(inspect(archive.read(member), f"{location}!{member}"))
        except zipfile.BadZipFile:
            findings.append(f"unreadable DOCX archive: {location}")
    return findings


def history_findings() -> list[str]:
    findings: list[str] = []
    commits = command("git", "rev-list", "--all").splitlines()
    for commit in commits:
        paths = command("git", "ls-tree", "-r", "--name-only", commit).splitlines()
        for path in paths:
            content = subprocess.run(
                ["git", "show", f"{commit}:{path}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            findings.extend(inspect_file(content, path, f"{commit[:12]}:{path}"))
    return findings


def worktree_findings() -> list[str]:
    findings: list[str] = []
    paths = command("git", "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    for path in paths:
        file = ROOT / path
        if not file.is_file():
            continue
        findings.extend(inspect_file(file.read_bytes(), path, f"worktree:{path}"))
    return findings


def main() -> int:
    findings = sorted(set(history_findings() + worktree_findings()))
    if findings:
        print("secret audit failed (values suppressed):")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("secret audit passed: Git history and worktree contain no non-placeholder secrets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
