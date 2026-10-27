"""Manage the official CLI and query exactly one host through a private registry."""

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import platform
import sys
import tarfile
import tempfile
import urllib.request
from urllib.parse import urlsplit
from collections.abc import Callable

from host_discovery import Host


class CLIError(Exception):
    pass


async def run_command(
    command: list[str], timeout: float, payload: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=timeout)
        if process.returncode is None:
            raise CLIError("CLI process did not report an exit code")
        return process.returncode, stdout, stderr
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()


def validate_release(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ValueError("invalid CLI release metadata")
    release = {}
    for key in ("version", "productVersion", "url", "sha256hash"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"CLI release metadata is missing {key}")
        release[key] = value
    address = urlsplit(release["url"])
    if address.scheme != "https" or not address.hostname or address.username or address.password:
        raise ValueError("CLI download URL must use HTTPS without embedded credentials")
    checksum = release["sha256hash"].lower()
    if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
        raise ValueError("CLI release metadata has an invalid SHA-256")
    release["sha256hash"] = checksum
    return release


def fetch_release() -> dict[str, str]:
    architecture = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower(),
    )
    if sys.platform != "linux" or architecture is None:
        raise ValueError("automatic CLI downloads support Linux x64 and arm64")
    url = f"https://update.code.visualstudio.com/api/update/cli-linux-{architecture}/stable/latest"
    with urllib.request.urlopen(url, timeout=30) as response:
        return validate_release(json.load(response))


def download_cli(path: Path, release: dict[str, str]) -> None:
    with urllib.request.urlopen(release["url"], timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != release["sha256hash"]:
        raise ValueError("CLI download SHA-256 verification failed")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.getmember("code")
        if not member.isfile():
            raise ValueError("CLI archive code entry must be a regular file")
        executable = bundle.extractfile(member)
        if executable is None:
            raise ValueError("CLI archive is missing executable contents")
        with executable, path.open("wb") as output:
            output.write(executable.read())
    path.chmod(0o755)


async def prepare_cli(
    path: Path, timeout: float, report: Callable[[str], None] = logging.warning,
) -> str:
    worker = [sys.executable, str(Path(__file__).resolve())]
    try:
        code, stdout, stderr = await run_command([*worker, "--release"], timeout)
        if code:
            raise CLIError(f"CLI release lookup failed: {stderr.decode(errors='replace').strip()}")
        release = validate_release(json.loads(stdout))
        commit = release["version"]
        if path.exists():
            try:
                code, version, _ = await run_command([str(path), "--version"], timeout)
            except OSError as error:
                report(f"installed CLI check failed; replacing it: {str(error) or type(error).__name__}")
            else:
                if code == 0 and commit in version.decode(errors="replace"):
                    return commit
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent, prefix=".code-update-") as temporary:
            candidate = Path(temporary) / "code"
            code, _, stderr = await run_command(
                [*worker, "--download", str(candidate)], 150, json.dumps(release).encode(),
            )
            if code:
                raise CLIError(f"CLI download failed: {stderr.decode(errors='replace').strip()}")
            code, version, _ = await run_command([str(candidate), "--version"], timeout)
            if code or commit not in version.decode(errors="replace"):
                raise CLIError("downloaded CLI does not match the latest stable commit")
            os.replace(candidate, path)
        return commit
    except (OSError, ValueError) as error:
        raise CLIError(f"CLI preparation failed: {str(error) or type(error).__name__}") from error


def validate_query(stdout: bytes, host: Host) -> None:
    try:
        records = json.loads(stdout)
    except (ValueError, UnicodeError) as error:
        raise CLIError("CLI query did not return valid JSON") from error
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise CLIError("CLI query must report exactly one selected host")
    record = records[0]
    identity = record.get("host")
    if (
        not isinstance(identity, dict) or type(identity.get("pid")) is not int
        or identity["pid"] != host.pid or identity.get("instanceId") != host.instance_id
    ):
        raise CLIError("CLI query returned a different host")
    if record.get("error") is not None:
        raise CLIError("CLI reported a host query error")
    if not isinstance(record.get("sessions"), list):
        raise CLIError("CLI query is missing a successful session list")


async def query_host(path: Path, host: Host, timeout: float) -> None:
    if not host.registration or not host.registry_name or Path(host.registry_name).name != host.registry_name:
        raise CLIError("selected host has no source registry entry")
    with tempfile.TemporaryDirectory(prefix="agent-host-cli-") as temporary:
        profile = Path(temporary)
        entries = profile / "agent-host" / "local-endpoint" / "entries"
        entries.mkdir(parents=True)
        entry = entries / host.registry_name
        with open(entry, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
            json.dump(host.registration, output)
        try:
            code, stdout, stderr = await run_command([
                str(path), "agent", "ps", "--json", "--user-data-dir", str(profile),
            ], timeout)
        except asyncio.TimeoutError as error:
            raise CLIError(f"CLI query timed out after {timeout:g}s") from error
        except OSError as error:
            raise CLIError(f"CLI query could not start: {host.safe_error(error)}") from error
        if code:
            detail = host.safe_error(RuntimeError(stderr.decode(errors="replace").strip()))[:500]
            raise CLIError(f"CLI query failed with exit code {code}" + (f": {detail}" if detail else ""))
        validate_query(stdout, host)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--release", action="store_true")
    action.add_argument("--download", type=Path)
    args = parser.parse_args()
    try:
        if args.release:
            print(json.dumps(fetch_release()))
        else:
            download_cli(args.download, validate_release(json.load(sys.stdin)))
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
