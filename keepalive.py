#!/usr/bin/env python3
"""Keep an AHP subscription connected using the latest stable VS Code CLI."""

import argparse
import fcntl
import hashlib
import io
import json
import os
import selectors
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_CLI = ROOT / "bin" / "code"
UPDATE_URL = "https://update.code.visualstudio.com/api/update/cli-linux-x64/stable/latest"
STOP = False


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message):
    print(f"{timestamp()} {message}", flush=True)


def write_state(path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps({"at": timestamp(), "pid": os.getpid(), **fields}) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def stop(_signal, _frame):
    global STOP
    STOP = True


def check_cli(path, commit):
    result = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=True)
    if commit not in result.stdout:
        raise ValueError(f"CLI at {path} does not match latest stable commit {commit}")


def update_cli(path):
    with urllib.request.urlopen(UPDATE_URL, timeout=30) as response:
        release = json.load(response)
    commit = release["version"]
    if path.exists():
        try:
            check_cli(path, commit)
            return commit
        except (OSError, subprocess.CalledProcessError, ValueError):
            log("installed CLI needs replacement; downloading latest stable release")
    if not release["url"].startswith("https://"):
        raise ValueError("CLI download URL must use HTTPS")
    with urllib.request.urlopen(release["url"], timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != release["sha256hash"]:
        raise ValueError("CLI download SHA-256 verification failed")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.getmember("code")
        if not member.isfile():
            raise ValueError("CLI archive code entry is not a regular file")
        executable = bundle.extractfile(member)
        if executable is None:
            raise ValueError("CLI archive is missing executable contents")
        with executable:
            binary = executable.read()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".code-", delete=False) as output:
            temporary = Path(output.name)
            output.write(binary)
        temporary.chmod(0o755)
        check_cli(temporary, commit)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    log(f"installed latest stable CLI {release['productVersion']} ({commit})")
    return commit


def watch(process, startup_timeout, on_connected):
    connected_at = None
    reason = "client-exited"
    buffer = b""
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + startup_timeout
    try:
        while not STOP:
            if connected_at is None and time.monotonic() >= deadline:
                return connected_at, "subscription-timeout"
            for _key, _events in selector.select(timeout=1):
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    return connected_at, reason
                buffer += chunk
                for line in buffer.split(b"\n")[:-1]:
                    if b"Streaming events (Ctrl+C to quit)" in line and connected_at is None:
                        connected_at = time.monotonic()
                        on_connected(process.pid)
                        log("AHP subscription connected")
                    elif connected_at is None and (
                        b"none of which are compatible" in line or b"AHP initialize failed" in line
                    ):
                        reason = "incompatible-protocol"
                    elif connected_at is None and (
                        b"authentication required" in line.lower() or b"sign in to" in line.lower()
                    ):
                        reason = "authentication-required"
                    elif connected_at is None and b"No agent for session" in line:
                        reason = "session-not-found"
                buffer = buffer.rsplit(b"\n", 1)[-1][-8192:]
            if process.poll() is not None and not selector.select(timeout=0):
                return connected_at, reason
        return connected_at, "stopped"
    finally:
        selector.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="AHP session URI, e.g. copilotcli:/<id>")
    parser.add_argument("--user-data-dir", type=Path, default=Path.home() / ".config" / "Code")
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI,
                        help="CLI executable (default: %(default)s)")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "run")
    parser.add_argument("--startup-timeout", type=int, default=45)
    args = parser.parse_args()
    if args.startup_timeout < 1:
        parser.error("--startup-timeout must be positive")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock = (args.state_dir / "keepalive.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error(f"another keepalive process holds {args.state_dir / 'keepalive.lock'}")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state = args.state_dir / "state.json"
    delay = 2
    while not STOP:
        try:
            commit = update_cli(args.cli)
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError, tarfile.TarError) as error:
            write_state(state, status="fatal", reason="cli-update-failed", detail=str(error))
            log(f"fatal: CLI update failed: {error}")
            return 2
        command = [
            str(args.cli), "agent", "logs", args.session,
            "--user-data-dir", str(args.user_data_dir),
        ]
        write_state(state, status="connecting", session=args.session, cli_commit=commit)
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as error:
            write_state(state, status="fatal", reason="cli-launch-failed", detail=str(error))
            log(f"fatal: CLI launch failed: {error}")
            return 2
        try:
            connected_at, reason = watch(
                process, args.startup_timeout,
                lambda pid: write_state(
                    state, status="connected", session=args.session, cli_commit=commit,
                    client_pid=pid, connected_at=timestamp(),
                ),
            )
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait()
            if process.stdout:
                process.stdout.close()

        if STOP:
            write_state(state, status="stopped", session=args.session)
            log("stopped")
            return 0
        if reason in {"incompatible-protocol", "authentication-required", "session-not-found"}:
            write_state(state, status="fatal", reason=reason, session=args.session)
            log(f"fatal: {reason}; update the CLI or session configuration")
            return 2
        if connected_at is not None and time.monotonic() - connected_at >= 60:
            delay = 2
        write_state(state, status="reconnecting", reason=reason, exit_code=exit_code,
                    session=args.session, retry_seconds=delay)
        log(f"subscription closed ({reason}, exit {exit_code}); reconnecting in {delay}s")
        for _ in range(delay):
            if STOP:
                break
            time.sleep(1)
        delay = min(delay * 2, 60)
    write_state(state, status="stopped", session=args.session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
